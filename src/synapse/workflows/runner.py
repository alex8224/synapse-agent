"""LangGraph-backed execution of one workflow script.

The generated script owns Python control flow; this module makes that control flow
durable.  Two things have to hold for a resumed run to be correct, and both are enforced
here:

1. **The orchestration checkpoint is the framework's job.**  The script body runs inside a
   LangGraph ``entrypoint``, and every agent call is a LangGraph ``task``.  A task whose
   result was committed is served from the checkpoint on resume, so the call is not
   executed again — the SDK's own record check is a second line of defence, not the
   primary mechanism.
2. **A task's input must be a plain JSON mapping.**  The checkpoint stores the payload, so
   the task takes :func:`~synapse.workflows.protocol.call_request_payload` output rather
   than the dataclass: a dict is what the framework's serializer is built to round-trip.

The runner does not run agents.  The injected executor is the seam: in the worker it sends
the call to the parent, and in tests it is a fake.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass, field
from typing import Any

from langgraph.func import entrypoint, task

from synapse.workflows.contract import CallRequest, WorkflowLimits, WorkflowStatus
from synapse.workflows.errors import (
    CallResultUncertainError,
    InvalidDraftError,
    WorkflowCancelledError,
)
from synapse.workflows.protocol import call_request_from_payload, call_request_payload
from synapse.workflows.recovery import plan_resume, reconcile_run
from synapse.workflows.sdk import ApprovalGate, CallExecutor, WorkflowSDK
from synapse.workflows.store import WorkflowStore, utcnow

__all__ = [
    "ENTRY_NAME",
    "ScriptEntry",
    "WorkflowRunner",
    "build_runner",
    "load_script",
]

#: The one entry point every generated script must define.
ENTRY_NAME = "run"


@dataclass(frozen=True, slots=True)
class ScriptEntry:
    """A compiled script and its ``run`` entry point."""

    run: Callable[..., Any]
    #: The script's own module namespace, kept so a failure can name what it defined.
    namespace: dict[str, Any] = field(default_factory=dict, repr=False)


def load_script(source: str) -> ScriptEntry:
    """Compile a generated script and return its entry point.

    This is *not* a sandbox and does not pretend to be one: executing the approved
    program is the whole point.  It does refuse a script that cannot compile or that
    defines no ``run``, so a broken draft fails before anything is dispatched.
    """
    if not isinstance(source, str) or not source.strip():
        raise InvalidDraftError("workflow script is empty")
    try:
        code = compile(source, "<workflow>", "exec")
    except SyntaxError as exc:
        detail = f"{exc.msg} (line {exc.lineno})"
        raise InvalidDraftError(f"workflow script does not compile: {detail}") from None
    namespace: dict[str, Any] = {"__name__": "synapse_workflow_script"}
    exec(code, namespace)  # noqa: S102 - running the approved program is the feature
    entry = namespace.get(ENTRY_NAME)
    if entry is None:
        raise InvalidDraftError(f"workflow script must define {ENTRY_NAME}(wf, inputs)")
    if not callable(entry):
        raise InvalidDraftError(f"workflow {ENTRY_NAME!r} must be callable")
    return ScriptEntry(run=entry, namespace=namespace)


@dataclass(slots=True)
class WorkflowRunner:
    """One script bound to its run identity, store and checkpointer."""

    run_id: str
    thread_id: str
    store: WorkflowStore
    limits: WorkflowLimits
    entry: ScriptEntry
    graph: Any
    clock: Callable[[], str] = utcnow

    def _config(self) -> dict[str, Any]:
        return {"configurable": {"thread_id": self.thread_id}}

    async def ainvoke(self, inputs: Any, *, durability: str = "sync") -> Any:
        """Execute (or resume) the script and settle the run's durable status.

        ``durability="sync"`` is deliberate: a task's result must be committed before the
        worker reports it, otherwise a crash could lose a completed call and the next
        attempt would repeat whatever it did.

        A workflow's inputs are a JSON object, and ``None`` is normalised to ``{}`` because
        LangGraph reads a ``None`` input as "resume with no new input" rather than as the
        script's own argument.
        """
        current = self.store.get_run(self.run_id)
        if current is not None and current.terminal:
            # A finished run is not re-executed: its stored outcome is the answer, and
            # running the program again could only repeat side effects.
            return self.store.run_result(self.run_id)
        # Decide from the run's own records *before* re-executing the program: an unknown
        # outcome is recorded as such, and a blocked resume fails here with a precise
        # reason instead of re-running the prologue and stopping partway.
        reconcile_run(self.store, self.run_id, reason="the run is resuming", at=self.clock())
        plan_resume(self.store, self.run_id).require_resumable()
        payload = {} if inputs is None else inputs
        try:
            return await self.graph.ainvoke(
                payload, self._config(), durability=durability
            )
        except asyncio.CancelledError:
            # The parent is shutting this worker down; it owns the status decision.
            raise
        except CallResultUncertainError as exc:
            self._settle(WorkflowStatus.UNCERTAIN, error=str(exc)[:2000])
            raise
        except WorkflowCancelledError as exc:
            self._settle(WorkflowStatus.CANCELLED, error=str(exc)[:2000])
            raise
        except BaseException as exc:
            self._settle(WorkflowStatus.FAILED, error=f"{type(exc).__name__}: {exc}"[:2000])
            raise

    def _settle(
        self, status: WorkflowStatus, *, result: Any = None, error: str | None = None
    ) -> None:
        """Move the run to a terminal-ish status, unless it is already there.

        A resumed run can reach the same outcome twice, and the state machine refuses a
        no-op transition, so an already-settled run is left alone.
        """
        current = self.store.get_run(self.run_id)
        if current is None or current.status is status:
            return
        if current.terminal:
            return
        self.store.set_run_status(
            self.run_id, status, result=result, error=error, at=self.clock()
        )


def build_runner(
    *,
    run_id: str,
    thread_id: str,
    script: str,
    store: WorkflowStore,
    limits: WorkflowLimits,
    checkpointer: Any,
    executor: CallExecutor,
    approval_gate: ApprovalGate | None = None,
    known_roles: Collection[str] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    clock: Callable[[], str] = utcnow,
) -> WorkflowRunner:
    """Assemble the durable orchestration around one script."""
    entry = load_script(script)

    def make_call_task(sdk: WorkflowSDK) -> Callable[[dict[str, Any]], Awaitable[Any]]:
        """Wrap the SDK's raw call path in a checkpointed LangGraph task.

        The closure is defined in this fixed place, so its identifier — and therefore the
        task id LangGraph derives from it — is identical in a later process.  That is what
        lets a resumed run find the committed result instead of re-executing the call.
        """

        async def execute_call(payload: dict[str, Any]) -> Any:
            return await sdk.execute_call(call_request_from_payload(payload))

        return task(execute_call, name="workflow_call")  # type: ignore[return-value]

    @entrypoint(checkpointer=checkpointer)
    async def run_workflow(inputs: Any) -> Any:
        sdk = WorkflowSDK(
            run_id=run_id,
            store=store,
            limits=limits,
            executor=executor,
            approval_gate=approval_gate,
            known_roles=known_roles,
            is_cancelled=is_cancelled,
            clock=clock,
        )
        call_task = make_call_task(sdk)
        sdk.set_call_runner(
            lambda request: _invoke_task(call_task, request)
        )
        value = entry.run(sdk, inputs)
        if inspect.isawaitable(value):
            value = await value
        await sdk.finish(value)
        return value

    return WorkflowRunner(
        run_id=run_id,
        thread_id=thread_id,
        store=store,
        limits=limits,
        entry=entry,
        graph=run_workflow,
        clock=clock,
    )


async def _invoke_task(call_task: Callable[[dict[str, Any]], Any], request: CallRequest) -> Any:
    """Await one task call, so the SDK sees a plain coroutine either way."""
    return await call_task(call_request_payload(request))
