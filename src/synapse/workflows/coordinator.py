"""Drive one workflow worker subprocess to a settled run.

This is the parent half of the protocol: it answers the worker's calls through an
injected handler, enforces the run's wall-clock limit, terminates the worker when asked,
and — the part that matters most — records an **honest** outcome when the worker does not
finish.  A worker that dies or is killed may already have changed files, so the run is
never reported as failed-and-retryable on a guess.

``on_call`` is the seam the real actor adapter plugs into: this module knows nothing about
models, tools or sessions.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from synapse.workflows import protocol
from synapse.workflows.contract import CallStatus, WorkflowStatus
from synapse.workflows.process import WorkerProcess, worker_stderr_summary
from synapse.workflows.protocol import WorkerConfig
from synapse.workflows.store import WorkflowStore, utcnow

__all__ = [
    "ApprovalHandler",
    "CallHandler",
    "EVENT_WORKER_EXIT",
    "WorkflowCoordinator",
    "WorkerOutcome",
]

#: Event kind carrying a bounded, sanitized diagnostic for an abnormal worker end.
#: Payload shape: ``{"phase": "startup" | "eof", "exit_code": int | None,
#: "stderr": [str, ...]}``.  ``phase`` distinguishes a worker that never started from one
#: that reached EOF; ``stderr`` holds only exception types and frame locations.
EVENT_WORKER_EXIT = "worker.exit"

#: Runs one agent call on the host side and returns its raw result.
CallHandler = Callable[[Any, bool], Awaitable[Any]]
#: Answers one business approval.
ApprovalHandler = Callable[[str, str], Awaitable[bool]]

#: How long the message loop waits before re-checking the clock and the cancel flag.
_POLL_SECONDS = 0.5

#: How long the host waits, off the event loop, for an exiting worker's code before
#: reporting ``None``.  EOF usually means the process is a moment from being reaped.
_EXIT_CODE_WAIT_SECONDS = 2.0

#: Exit code the worker uses for a start message it could not accept.
_EXIT_BAD_CONFIG = 3


@dataclass(frozen=True, slots=True)
class WorkerOutcome:
    """What one worker invocation amounted to."""

    status: WorkflowStatus
    value: Any = None
    error: str | None = None
    exit_code: int | None = None

    @property
    def completed(self) -> bool:
        return self.status is WorkflowStatus.COMPLETED


@dataclass(slots=True)
class WorkflowCoordinator:
    """One run, one worker, one settled status."""

    store: WorkflowStore
    config: WorkerConfig
    on_call: CallHandler
    on_approval: ApprovalHandler | None = None
    process_factory: Callable[[WorkerConfig], WorkerProcess] = WorkerProcess
    clock: Callable[[], str] = utcnow
    #: Call keys handed to the host that have not been answered yet.
    _inflight: dict[str, str] = field(default_factory=dict)
    _tasks: set[asyncio.Task[None]] = field(default_factory=set)
    _cancelled: bool = False
    _cancel_reason: str = "user"
    _worker: WorkerProcess | None = None

    async def run(self) -> WorkerOutcome:
        """Start the worker and drive it until the run settles."""
        worker: WorkerProcess | None = None
        deadline = time.monotonic() + float(self.config.limits.max_seconds)
        try:
            if self._cancelled:
                return self._settle_cancelled(worker)
            try:
                worker = self.process_factory(self.config)
                self._worker = worker
                worker.start()
            except Exception as exc:  # process startup boundary: persist an honest outcome
                if self._cancelled:
                    return self._settle_cancelled(worker)
                # A start may fail after sending the program. Only a missing PID proves
                # that no worker ran; otherwise retain uncertainty instead of inviting a retry.
                started = worker is not None and worker.pid is not None
                status = WorkflowStatus.UNCERTAIN if started else WorkflowStatus.FAILED
                if started:
                    worker.kill()
                error = f"workflow worker could not start ({type(exc).__name__})"
                self._record_worker_exit(
                    "startup",
                    None if worker is None else worker.returncode,
                    await self._drain_stderr(worker),
                )
                if self._cancelled:
                    return self._settle_cancelled(worker)
                self._settle(status, error=error)
                return WorkerOutcome(
                    status=status, error=error,
                    exit_code=None if worker is None else worker.returncode,
                )
            return await self._drive(worker, deadline)
        finally:
            for task in list(self._tasks):
                task.cancel()
            if self._tasks:
                await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()
            if worker is not None:
                worker.close()
            self._worker = None

    def cancel(self, reason: str = "user") -> None:
        """Stop the run: kill the worker and record a cancelled status.

        Safe to call from another task or thread; the message loop observes the flag.
        """
        self._cancelled = True
        self._cancel_reason = str(reason or "user")
        worker = self._worker
        if worker is not None:
            worker.kill()

    # -- message loop ------------------------------------------------------

    async def _drive(self, worker: WorkerProcess, deadline: float) -> WorkerOutcome:
        while True:
            if self._cancelled:
                return self._settle_cancelled(worker)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return (
                    self._settle_cancelled(worker)
                    if self._cancelled
                    else self._settle_timeout(worker)
                )
            slice_s = min(_POLL_SECONDS, max(0.05, remaining))
            try:
                message = await asyncio.to_thread(worker.next_message, timeout=slice_s)
            except TimeoutError:
                continue
            if message is None:
                # A cancel kills the worker, so the EOF arrives before the next flag
                # check: the flag decides what this exit means, not the exit code.
                if self._cancelled:
                    return self._settle_cancelled(worker)
                return await self._settle_exit(worker)
            kind = message.get("type")
            if kind == protocol.KIND_CALL:
                self._inflight[str(message.get("call_key"))] = str(message.get("id"))
                self._spawn(self._answer_call(worker, message))
            elif kind == protocol.KIND_APPROVAL:
                self._spawn(self._answer_approval(worker, message))
            elif kind == protocol.KIND_RESULT:
                return self._settle_result(worker, message)
            elif kind == protocol.KIND_ERROR:
                return self._settle_error(worker, message)
            # Anything else is a frame this host does not know: ignore it and keep
            # reading, because dropping the worker would lose a run over a stray line.

    def _spawn(self, coroutine: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _answer_call(self, worker: WorkerProcess, message: dict[str, Any]) -> None:
        request_id = str(message.get("id"))
        call_key = str(message.get("call_key"))
        try:
            request = protocol.call_request_from_payload(message)
            value = await self.on_call(request, bool(message.get("correction")))
            reply: dict[str, Any] = {
                "type": protocol.KIND_CALL_RESULT,
                "id": request_id,
                "value": value,
            }
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            # Keep the failure's class on the wire so the worker can rebuild it: a format
            # error earns one corrective attempt, an ordinary error does not.
            reply = protocol.call_error_message(request_id, exc)
        finally:
            self._inflight.pop(call_key, None)
        try:
            worker.send(reply)
        except (OSError, ValueError):
            # The worker is gone; the loop reports the exit.  The call record stays as the
            # SDK left it, which is exactly the uncertain window a resume must handle.
            return

    async def _answer_approval(self, worker: WorkerProcess, message: dict[str, Any]) -> None:
        granted = False
        if self.on_approval is not None:
            granted = bool(
                await self.on_approval(str(message.get("key")), str(message.get("description")))
            )
        try:
            worker.send(
                {
                    "type": protocol.KIND_APPROVAL_RESULT,
                    "id": str(message.get("id")),
                    "granted": granted,
                }
            )
        except (OSError, ValueError):
            return

    # -- settlement --------------------------------------------------------

    def _settle_result(self, worker: WorkerProcess, message: dict[str, Any]) -> WorkerOutcome:
        value = message.get("value")
        # The runner settles the run before reporting; this is the fallback for a store
        # write that did not land, and it must not overwrite a different outcome.
        self._settle(WorkflowStatus.COMPLETED, result=value)
        return WorkerOutcome(
            status=WorkflowStatus.COMPLETED, value=value, exit_code=worker.returncode
        )

    def _settle_error(self, worker: WorkerProcess, message: dict[str, Any]) -> WorkerOutcome:
        error = str(message.get("error") or "workflow failed")
        status = (
            WorkflowStatus.UNCERTAIN if message.get("uncertain") else WorkflowStatus.FAILED
        )
        self._settle(status, error=error)
        return WorkerOutcome(status=status, error=error, exit_code=worker.returncode)

    def _settle_cancelled(self, worker: WorkerProcess | None) -> WorkerOutcome:
        if worker is not None:
            worker.kill()
        self._mark_inflight_uncertain("the workflow was cancelled while this call ran")
        # Two steps, because the state machine does not jump straight to cancelled.
        self._settle(WorkflowStatus.CANCELLING)
        self._settle(WorkflowStatus.CANCELLED, error=f"cancelled by {self._cancel_reason}")
        return WorkerOutcome(
            status=WorkflowStatus.CANCELLED,
            error=f"cancelled by {self._cancel_reason}",
            exit_code=None if worker is None else worker.returncode,
        )

    def _settle_timeout(self, worker: WorkerProcess) -> WorkerOutcome:
        worker.kill()
        self._mark_inflight_uncertain("the workflow stopped while this call was running")
        reason = f"workflow exceeded its time limit ({self.config.limits.max_seconds:g}s)"
        self._settle(WorkflowStatus.FAILED, error=reason)
        return WorkerOutcome(
            status=WorkflowStatus.FAILED, error=reason, exit_code=worker.returncode
        )

    async def _settle_exit(self, worker: WorkerProcess) -> WorkerOutcome:
        """The worker is gone without reporting an outcome."""
        code = await self._await_exit_code(worker)
        # A cancel can land while the exit code is awaited off the loop; the flag decides
        # what this exit means, not the code.  Reporting uncertainty here would mislabel an
        # explicit cancellation as an unknown outcome.
        if self._cancelled:
            return self._settle_cancelled(worker)
        self._record_worker_exit("eof", code, await self._drain_stderr(worker))
        if self._cancelled:
            return self._settle_cancelled(worker)
        if code == _EXIT_BAD_CONFIG:
            reason = "the workflow worker refused its configuration"
            self._settle(WorkflowStatus.FAILED, error=reason)
            return WorkerOutcome(status=WorkflowStatus.FAILED, error=reason, exit_code=code)
        # Anything else may have run code with side effects: report uncertainty rather
        # than a failure that invites a retry.
        reason = f"the workflow worker exited unexpectedly (code {code})"
        self._mark_inflight_uncertain(reason)
        self._settle(WorkflowStatus.UNCERTAIN, error=reason)
        return WorkerOutcome(status=WorkflowStatus.UNCERTAIN, error=reason, exit_code=code)

    async def _await_exit_code(self, worker: WorkerProcess) -> int | None:
        """Wait, briefly and off the event loop, for the worker's exit code.

        EOF on the protocol pipe usually means the process is a moment from being reaped,
        so a short bounded wait turns a racy ``None`` into the real code without ever
        blocking the Agent loop.  A worker that somehow outlives the wait keeps ``None``:
        the run is uncertain either way, and inventing a code would be worse than none.
        """
        code = worker.returncode
        if code is not None:
            return code
        try:
            return await asyncio.to_thread(worker.wait, timeout=_EXIT_CODE_WAIT_SECONDS)
        except TimeoutError:
            return worker.returncode
        except Exception:  # noqa: BLE001 - a broken wait must not leave the run unsettled
            return worker.returncode

    def _record_worker_exit(
        self, phase: str, code: int | None, stderr: list[str]
    ) -> None:
        """Persist a bounded, sanitized diagnostic for an abnormal worker end.

        Raw stderr, exception messages and anything the script printed are never written:
        this lands in the durable event log, where a secret or payload must not survive.
        ``stderr`` is already reduced to exception types and frame locations.  A diagnostic
        that cannot be written must not change the run's real outcome.
        """
        try:
            self.store.append_event(
                self.config.run_id,
                EVENT_WORKER_EXIT,
                {"phase": phase, "exit_code": code, "stderr": stderr},
                at=self.clock(),
            )
        except Exception:  # noqa: BLE001 - bookkeeping must not hide the real outcome
            return

    async def _drain_stderr(self, worker: WorkerProcess | None) -> list[str]:
        """Snapshot a worker's stderr as a bounded, safe diagnostic, off the event loop.

        The stderr reader runs in its own thread, so the join that makes the snapshot
        complete is run through ``to_thread``: the Agent loop is never blocked by a worker's
        output, and the join itself is bounded.  Only a traceback's exception type and frame
        locations survive ``worker_stderr_summary``; the raw text never reaches the log.
        """
        if worker is None:
            return []
        try:
            lines = await asyncio.to_thread(worker.drain_stderr)
        except Exception:  # noqa: BLE001 - a diagnostic must not hide the outcome
            try:
                lines = worker.stderr_tail()
            except Exception:  # noqa: BLE001 - even the fallback is best effort
                return []
        try:
            return worker_stderr_summary(lines)
        except Exception:  # noqa: BLE001 - a diagnostic must not hide the outcome
            return []

    def _mark_inflight_uncertain(self, reason: str) -> None:
        """Record the calls this host answered nothing for as unknown outcomes."""
        for call_key in list(self._inflight):
            try:
                self.store.mark_call_uncertain(
                    self.config.run_id, call_key, reason=reason, at=self.clock()
                )
            except Exception:  # noqa: BLE001 - bookkeeping must not hide the real outcome
                continue
        self._inflight.clear()

    def _settle(
        self,
        status: WorkflowStatus,
        *,
        result: Any = None,
        error: str | None = None,
    ) -> None:
        """Move the run, leaving an already-settled run alone."""
        current = self.store.get_run(self.config.run_id)
        if current is None or current.status is status:
            return
        try:
            self.store.set_run_status(
                self.config.run_id, status, result=result, error=error, at=self.clock()
            )
        except Exception:  # noqa: BLE001 - the reported outcome still stands
            return


def inflight_call_keys(store: WorkflowStore, run_id: str) -> list[str]:
    """Call keys still recorded as running (diagnostics for a resume)."""
    return [
        record.call_key
        for record in store.list_calls(run_id)
        if record.status is CallStatus.RUNNING
    ]
