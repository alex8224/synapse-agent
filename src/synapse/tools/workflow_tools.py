"""Workflow tools: allow the AI Agent to author, run and inspect dynamic workflows.

Follows the ZCode / Synapse architectural discipline:
- Workflows are model-generated Python programs executed in an isolated worker subprocess;
- The tool pre-checks script syntax and function signature with ``load_script``;
- The script is automatically saved to ``.synapse/workflows/drafts/<name>.py`` for persistence;
- Execution is tracked durably in SQLite with tokens and step history, and reported back.

``create_workflow`` is synchronous: it starts the run and waits for the final outcome.
``cancel_workflow_run`` stops a run through the service (never by editing the database) and
does not roll back side effects.  Actor roles are read from the project's enabled registry
and never invented here; the description only names roles an already-built service reports.
"""

from __future__ import annotations

import ast
import json
import logging
import re
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

from synapse.observability.error_log import safe_error_text
from synapse.workflows.contract import WorkflowDraft, WorkflowLimits, WorkflowStatus
from synapse.workflows.coordinator import EVENT_WORKER_EXIT
from synapse.workflows.errors import InvalidDraftError
from synapse.workflows.records import WorkflowEvent
from synapse.workflows.runner import ENTRY_NAME, load_script
from synapse.workflows.service import WorkflowService

logger = logging.getLogger(__name__)

CREATE_WORKFLOW_NAME = "create_workflow"
GET_WORKFLOW_RUN_NAME = "get_workflow_run"
LIST_WORKFLOW_RUNS_NAME = "list_workflow_runs"
CANCEL_WORKFLOW_RUN_NAME = "cancel_workflow_run"

#: A workflow name becomes a draft filename, so it must not be able to escape the drafts
#: directory: no separators, no ``..``, not absolute.
_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

#: Bounded window of recent events the workflow tools may display.
_MAX_EVENT_LINES = 12
#: Ordinary event payloads may embed user data or secrets, so their rendered text is
#: redacted and kept short.
_MAX_EVENT_PAYLOAD_CHARS = 200
#: A ``worker.exit`` payload is a structural summary (exception types and frame
#: locations, already sanitized where it is written), so it keeps more of its text: a
#: frame location clipped mid-path is exactly the detail a reader needs.
_MAX_WORKER_EXIT_CHARS = 600


def _valid_workflow_name(name: Any) -> bool:
    """Whether ``name`` is safe to use as a draft filename."""
    if not isinstance(name, str) or not name or ".." in name:
        return False
    # ``fullmatch`` (not ``match``) so a trailing newline cannot slip past ``$``.
    return _NAME_PATTERN.fullmatch(name) is not None


def _entry_function(tree: ast.Module) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """The module-level ``run`` definition the loader would call, if it is a ``def``.

    The loader ``exec``s the module and looks up the ``run`` name, so the *last*
    module-level binding is the one that runs -- not the first.  Only that definition's
    first parameter can be the SDK handle; scanning the whole tree instead would mistake an
    unrelated helper's coincidentally-named parameter for the workflow SDK.
    """
    bound: ast.AST | None = None
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == ENTRY_NAME:
            bound = node
            continue
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        if any(
            isinstance(sub, ast.Name) and sub.id == ENTRY_NAME
            for target in targets
            for sub in ast.walk(target)
        ):
            # ``run`` is rebound to something that is not a definition, so the entry point
            # cannot be analysed statically: report nothing rather than the wrong function.
            bound = None
    return bound if isinstance(bound, (ast.FunctionDef, ast.AsyncFunctionDef)) else None


def _shadows_sdk(node: ast.AST, sdk_name: str) -> bool:
    """Whether a nested definition rebinds the SDK name before its body runs."""
    if isinstance(node, ast.ClassDef):
        return node.name == sdk_name
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    args = node.args
    names = {arg.arg for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
    names.update(a.arg for a in (args.vararg, args.kwarg) if a is not None)
    return node.name == sdk_name or sdk_name in names


def _sdk_actor_calls(node: ast.AST, sdk_name: str) -> Iterator[ast.Call]:
    """Yield ``<sdk_name>.actor(...)`` calls that clearly use the workflow SDK.

    Walks the entry point's body but never descends into a nested function or class that
    shadows the SDK name, because there the name no longer refers to the SDK.
    """
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if _shadows_sdk(child, sdk_name):
                continue
        if isinstance(child, ast.Call):
            func = child.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "actor"
                and isinstance(func.value, ast.Name)
                and func.value.id == sdk_name
            ):
                yield child
        yield from _sdk_actor_calls(child, sdk_name)


def _literal_role_args(call: ast.Call) -> set[str]:
    """String-literal roles in one ``actor(...)`` call (positional or ``role=``)."""
    roles: set[str] = set()
    if call.args:
        first = call.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str) and first.value.strip():
            roles.add(first.value)
    for keyword in call.keywords:
        if keyword.arg != "role":
            continue
        value = keyword.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str) and value.value.strip():
            roles.add(value.value)
    return roles


def _literal_actor_roles(source: str) -> set[str]:
    """Literal roles used by ``<sdk>.actor("role", ...)`` calls in a script.

    Only string literals are reported: a dynamic role expression cannot be checked
    statically and is validated by the runtime instead.  Calls are matched on the ``run``
    entry point's first parameter, and the scan is scoped to that function's body (skipping
    nested definitions that shadow the SDK name), so an unrelated ``something.actor(...)``
    -- including one on a helper whose parameter happens to be named like the SDK -- is
    never mistaken for a workflow SDK call.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    entry = _entry_function(tree)
    if entry is None:
        return set()
    args = [*entry.args.posonlyargs, *entry.args.args]
    if not args:
        return set()
    sdk_name = args[0].arg
    roles: set[str] = set()
    for call in _sdk_actor_calls(entry, sdk_name):
        roles |= _literal_role_args(call)
    return roles


def _format_roles(roles: Any) -> str:
    return ", ".join(f"`{role}`" for role in sorted(set(roles)))


def _roles_description(service: WorkflowService | None) -> str:
    """Role guidance for the create-tool description.

    Reads roles from an *already-built* service only.  Building the lazy service can open
    a database and assemble actor resources, so the description must never trigger it.
    """
    if service is None:
        return (
            "Actor roles come from the project's enabled agent-role registry (this tool "
            "does not define them). Use the role names the project has enabled; a literal "
            "role outside that set is rejected before the run starts, and a dynamic role "
            "expression is validated at run time."
        )
    roles = tuple(service.resources.roles)
    if not roles:
        return (
            "This project has no enabled agent roles, so no actor can be dispatched: a "
            "script that calls wf.actor(...) is rejected until roles are enabled or "
            "registered in the project's agent configuration."
        )
    return (
        f"Enabled roles for this project: {_format_roles(roles)}. Use only these roles; a "
        "literal role outside this set is rejected before the run starts, and a dynamic "
        "role expression is validated at run time."
    )


def _role_precheck(
    available: tuple[str, ...], declared: list[str] | None, source: str
) -> str | None:
    """Refuse a script that names a role the project has not enabled.

    Checks both the roles the caller declared and the literal roles the script uses in
    ``wf.actor(...)``.  A dynamic role expression is left to the runtime.  Returns a
    message to hand back to the model, or ``None`` when the roles are acceptable.
    """
    enabled = tuple(available)
    declared_set = {role for role in (declared or []) if isinstance(role, str) and role.strip()}
    used = _literal_actor_roles(source) | declared_set
    if not enabled:
        if used:
            return (
                "No agent roles are enabled for this project, so no actor can be "
                f"dispatched; the script references {_format_roles(used)}. Enable or "
                "register agent roles in the project's agent configuration, then retry. "
                "No draft was written and no run was created."
            )
        return None
    unknown = used - set(enabled)
    if unknown:
        return (
            "Workflow references role(s) that are not enabled for this project: "
            f"{_format_roles(unknown)}. Available roles: {_format_roles(enabled)}. Use "
            "only enabled roles. No draft was written and no run was created."
        )
    return None


def _active_run_block(run: Any) -> str:
    """Explain the active run that blocks a new create request."""
    lines = [
        f"This project already has an active workflow run: {run.run_id} "
        f"(status: {run.status.value}). Only one workflow runs per project at a time, so "
        "this request was refused before writing a draft or creating a run.",
    ]
    if run.status is WorkflowStatus.UNCERTAIN:
        lines.append(
            "Its outcome is uncertain (it stopped without reporting a result, which can "
            "happen even with no actor call recorded) and it is not retried or rebuilt "
            "automatically."
        )
    lines.append(
        "Inspect it with get_workflow_run(run_id=...) and, after confirming whether its "
        "side effects already happened, cancel it with cancel_workflow_run(run_id=...) to "
        "release the project. Cancelling does not roll back side effects."
    )
    return "\n".join(lines)


def _uncertain_recovery(run_id: str) -> str:
    return (
        f"Recovery: run {run_id} has no established outcome, so it is not retried or "
        "rebuilt automatically. Review the calls above, decide whether the side effects "
        "already happened, then either cancel it with cancel_workflow_run(run_id=...) to "
        "release the project slot, or leave it as evidence. Cancelling does not roll back "
        "side effects."
    )


def _render_event_detail(event: WorkflowEvent) -> str:
    """One event payload rendered for display, bounded and safe to show.

    A ``worker.exit`` payload is the worker's structural summary, already sanitized where
    it was written, so it keeps more of its text.  Any other event may embed a user
    payload or a secret, so its text is redacted through the shared error sanitizer.
    """
    if event.payload is None:
        return ""
    try:
        detail = json.dumps(event.payload, ensure_ascii=False, sort_keys=True)
    except Exception:  # noqa: BLE001 - a payload need not be JSON-serialisable
        detail = str(event.payload)
    if event.kind == EVENT_WORKER_EXIT:
        return safe_error_text(detail, limit=_MAX_WORKER_EXIT_CHARS)
    return safe_error_text(detail, limit=_MAX_EVENT_PAYLOAD_CHARS)


def _recent_events(srv: WorkflowService, run_id: str) -> list[str]:
    """A bounded, value-light view of the run's most recent durable events.

    Reads only the tail, so a long run's final events (notably a ``worker.exit``
    diagnostic) are shown instead of being hidden behind a forward read window.
    """
    try:
        events = srv.resources.store.read_recent_events(run_id, _MAX_EVENT_LINES)
    except Exception:  # noqa: BLE001 - diagnostics must never fail the read
        return []
    lines: list[str] = []
    for event in events:
        detail = _render_event_detail(event)
        suffix = f" {detail}" if detail else ""
        lines.append(f"  - [{event.sequence}] {event.kind}{suffix}")
    return lines


def _build_create_description(service: WorkflowService | None) -> str:
    """The full model-facing description for ``create_workflow``."""
    return (
        "Create, compile and execute a dynamic multi-agent workflow, then wait for it to "
        "settle and return its result.\n\n"
        "This tool is synchronous: it starts the run and waits for the final outcome "
        "(status, calls, token usage and result). It does not return early and does not "
        "run the workflow in the background.\n\n"
        "When to use:\n"
        "- When the user explicitly asks for a workflow (\"use a workflow\", \"使用工作流\", "
        "\"通过工作流\", \"workflow\").\n"
        "- Complex multi-agent engineering tasks that require formal verification gates, "
        "loops, and review/test/rework cycles (such as a full bug-fix verification loop "
        "where an implementer fixes and a tester verifies).\n"
        "- Without an explicit workflow request, do not start a workflow for simple "
        "single-agent queries; use standard tools or the task tool instead.\n\n"
        "Authoring rules for `script`:\n"
        "- Must be valid Python defining an async entry point: `async def run(wf, inputs):`\n"
        "- Instantiate actors: `actor = wf.actor(role, key=\"actor_key\", readonly=True/False)`. "
        "The same key preserves context across calls.\n"
        "- readonly=True disables ALL shell execution, including git diff and pytest. "
        "For read-only reviews, collect the diff with an authorized tool first and pass it "
        "as actor input; do not ask a shell-less actor to run git. Test execution needs "
        "readonly=False and a role/project policy that permits execute. Never change "
        "permissions automatically to bypass a restriction.\n"
        "- Actors inherit project tool restrictions. If no inspection tools remain, use "
        "input-only analysis with supplied evidence or report the missing capability. "
        "Do not repeatedly create workflows or remove schema to hide a tool-call failure.\n"
        f"- {_roles_description(service)}\n"
        "- Ask an actor: `res = await actor.ask(key=\"call_key\", prompt=\"...\", "
        "schema=SCHEMA)`. If the answer does not match the schema, the run makes one "
        "format-only corrective attempt with all tools disabled before failing the call.\n"
        "- Parallel execution: `results = await wf.gather([call1, call2, ...])`.\n"
        "- Progress reporting: `wf.progress(stage=\"stage_name\", detail=\"detail_message\")`.\n"
        "- Return value: `return {...}` becomes the workflow's final result.\n\n"
        "Args:\n"
        "    name: Identifier for the workflow (e.g. \"code_review\"): letters, digits, "
        "'_', '-' or '.', with no path separators or '..'.\n"
        "    title: Human-readable title describing what the workflow does.\n"
        "    goal: Concrete goal of the workflow run.\n"
        "    script: Full Python workflow script inline. Exactly one of `script` and "
        "`script_path` must be provided.\n"
        "    script_path: Path to an existing workflow Python script file on disk.\n"
        "    inputs: Optional dictionary of inputs passed into `run(wf, inputs)`.\n"
        "    roles: Roles this workflow uses. Defaults to the project's enabled roles; "
        "every role declared here or used literally in the script must be enabled.\n"
        "    max_calls: Safety cap on total actor calls (default: 10).\n"
        "    max_actors: Safety cap on distinct actors (default: 5).\n"
        "    max_seconds: Safety timeout in seconds (default: 300).\n"
    )


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

    @tool(description=_build_create_description(service))
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
        """Create, compile and execute a dynamic workflow, then return its result.

        See the tool description for authoring rules and the project's enabled roles.
        """
        srv = _resolve_service()
        if srv is None:
            return "Workflow service is not available for this project."

        ws = _resolve_workspace()

        # 1. The name becomes a draft filename: refuse anything that could escape it.
        if not _valid_workflow_name(name):
            return (
                "Invalid workflow 'name': use letters, digits, '_', '-' or '.', with no "
                "path separators or '..'."
            )

        # 2. One active run per project: refuse before writing a draft or creating a run.
        active = srv.active_run()
        if active is not None:
            return _active_run_block(active)

        # 3. Resolve source
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

        # 4. Syntax pre-check
        try:
            load_script(source)
        except InvalidDraftError as exc:
            return (
                f"Workflow script failed pre-compilation check:\n{exc}\n"
                "Please fix the script syntax or ensure `async def run(wf, inputs):` is defined."
            )

        # 5. Role pre-check: declared roles plus literal roles used in the script.
        role_error = _role_precheck(srv.resources.roles, roles, source)
        if role_error is not None:
            return role_error

        # 6. Auto-save draft file to disk for persistence and manual inspection
        drafts_dir = ws / ".synapse" / "workflows" / "drafts"
        try:
            drafts_dir.mkdir(parents=True, exist_ok=True)
            draft_file = drafts_dir / f"{name}.py"
            draft_file.write_text(source, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.warning("failed to write workflow draft file: %s", exc)
            draft_file = drafts_dir / f"{name}.py"

        # 7. Save and approve draft
        limits = WorkflowLimits(
            max_calls=max(1, int(max_calls)),
            max_actors=max(1, int(max_actors)),
            max_seconds=max(10.0, float(max_seconds)),
        )
        declared = [role for role in (roles or []) if isinstance(role, str) and role.strip()]
        effective_roles = tuple(dict.fromkeys(declared)) if declared else srv.resources.roles
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

        # 8. Create and run
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

        # 9. Read outcomes and format response
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

        # An uncertain or failed run is exactly when the caller needs the diagnostics, so
        # attach them here instead of making the caller infer the cause with get_workflow_run.
        if outcome.status in (WorkflowStatus.UNCERTAIN, WorkflowStatus.FAILED):
            if outcome.status is WorkflowStatus.UNCERTAIN:
                lines.append(_uncertain_recovery(run.run_id))
            else:
                lines.append(
                    f"Run {run.run_id} failed and released the project's workflow slot, "
                    "so a corrected workflow can be created once the cause is addressed."
                )
            events = _recent_events(srv, run.run_id)
            if events:
                lines.append(f"Recent events ({len(events)}):")
                lines.extend(events)

        return "\n".join(lines)

    @tool
    def get_workflow_run(run_id: str) -> str:
        """Inspect one workflow run: status, calls, token usage and result (read-only).

        When a run is ``uncertain`` its outcome is not established; this tool explains how
        to recover and never retries or rebuilds the run.

        Args:
            run_id: The ID of the workflow run to inspect.
        """
        srv = _resolve_service()
        if srv is None:
            return "Workflow service is not available for this project."

        run = srv.get_run(run_id)
        if run is None:
            return f"Workflow run '{run_id}' not found."
        if run.project_id != srv.resources.project_id:
            # Do not confirm another project's run exists; report it as not found here.
            return f"Workflow run '{run_id}' not found for the current project."

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
        if run.status is WorkflowStatus.UNCERTAIN:
            lines.append(_uncertain_recovery(run_id))

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

        events = _recent_events(srv, run_id)
        if events:
            lines.append(f"Recent events ({len(events)}):")
            lines.extend(events)

        return "\n".join(lines)

    @tool
    def cancel_workflow_run(run_id: str) -> str:
        """Request cancellation of one workflow run for the current project.

        Use this after inspecting a run with ``get_workflow_run`` and confirming it should
        stop — including an ``uncertain`` run, which otherwise keeps the project's single
        workflow slot occupied. Cancelling stops the worker and marks the run cancelled; it
        does **not** roll back side effects the script or its actors already performed. Only
        runs of the current project can be cancelled, and an unknown or already-terminal run
        is reported as a no-op, so a repeated cancel is safe.

        Args:
            run_id: The ID of the workflow run to cancel.
        """
        srv = _resolve_service()
        if srv is None:
            return "Workflow service is not available for this project."

        run = srv.get_run(run_id)
        if run is None:
            return f"Workflow run '{run_id}' not found for this project; nothing to cancel."
        if run.project_id != srv.resources.project_id:
            return (
                f"Workflow run '{run_id}' does not belong to the current project; "
                "refusing to cancel it."
            )
        if run.terminal:
            return (
                f"Workflow run '{run_id}' is already {run.status.value}; nothing to cancel "
                "(no-op). Its side effects are not rolled back."
            )

        srv.cancel(run_id, reason="cancelled by the agent tool")
        current = srv.get_run(run_id) or run
        return (
            f"Cancellation requested for workflow run '{run_id}'; status is now "
            f"{current.status.value}. The run's side effects are not rolled back, and the "
            "project's workflow slot is released once it reaches 'cancelled'."
        )

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

    return [create_workflow, get_workflow_run, list_workflow_runs, cancel_workflow_run]
