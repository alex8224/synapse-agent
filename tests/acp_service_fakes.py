"""Process-free fakes for ACP tests at the AgentRuntimeService boundary."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from synapse.acp.sessions import ACPManagedSession, ACPSessionDescriptor
from synapse.runtime.service.commands import (
    CancelTurnCommand,
    CancelTurnResult,
    CloseSessionCommand,
    CloseSessionResult,
    CommandReceipt,
    ResumeTurnCommand,
    ResumeTurnResult,
    SubmitTurnCommand,
)
from synapse.runtime.service.events import RuntimeEvent
from synapse.runtime.service.queries import (
    ApprovalActionView,
    GetSessionQuery,
    PendingApprovalQuery,
    PendingApprovalView,
    SessionView,
    UsageView,
)
from synapse.runtime.sessions.ref import SessionRef


@dataclass(frozen=True)
class FakeOutcome:
    turn_id: str = "turn-1"
    status: str = "completed"
    final_text: str = "done"
    usage: Any = None


class _Watch:
    def __init__(self, service: FakeAgentRuntimeService, after: int) -> None:
        self.service = service
        self.after = after
        self.queue: asyncio.Queue[RuntimeEvent | None] = asyncio.Queue()
        self.closed = False

    async def __aenter__(self) -> _Watch:
        self.service.watch_observed = True
        self.service.calls.append(("watch-enter", self.after))
        self.service._watchers.append(self)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self.closed = True
        if self in self.service._watchers:
            self.service._watchers.remove(self)
        self.service.calls.append("watch-exit")

    def __aiter__(self) -> _Watch:
        return self

    async def __anext__(self) -> RuntimeEvent:
        event = await self.queue.get()
        if event is None:
            raise StopAsyncIteration
        return event


class FakeAgentRuntimeService:
    """Small deterministic service fake; no runtime manager, task handle, or process."""

    def __init__(
        self,
        outcomes: Iterable[FakeOutcome] | None = None,
        events: Iterable[RuntimeEvent] | None = None,
        *,
        blocking: bool = False,
        submit_error: BaseException | None = None,
        resume_error: BaseException | None = None,
        approval_actions: Iterable[ApprovalActionView] | None = None,
    ) -> None:
        self.outcomes = list(outcomes or [FakeOutcome()])
        self.event_templates = list(events or [])
        self.calls: list[Any] = []
        self._watchers: list[_Watch] = []
        self.watch_observed = False
        self.active_turn_id: str | None = None
        self.current_outcome: FakeOutcome | None = None
        self.blocking = blocking
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        if not blocking:
            self.release.set()
        self.submit_error = submit_error
        self.resume_error = resume_error
        self.approval_actions = None if approval_actions is None else tuple(approval_actions)
        self.view = SessionView("project", "thread", "idle", None, 0, UsageView(), None, "")

    def watch_events(self, session: SessionRef, *, after: int = 0, **_: Any) -> _Watch:
        self.calls.append(("watch", session, after))
        return _Watch(self, after)

    async def open_session(self, command: Any) -> Any:
        self.calls.append(command)
        return SimpleNamespace(
            command_id=command.command_id,
            session=command.session,
            created=True,
            view=self.view,
        )

    async def get_session(self, query: GetSessionQuery) -> SessionView:
        self.calls.append(query)
        return self.view

    async def pending_approval(self, query: PendingApprovalQuery) -> PendingApprovalView:
        self.calls.append(query)
        actions = ()
        if self.current_outcome is not None and self.current_outcome.status == "waiting_approval":
            actions = self.approval_actions
            if actions is None:
                actions = (ApprovalActionView(0, "approval", {}),)
        return PendingApprovalView(query.expected_turn_id, actions)

    async def submit_turn(self, command: SubmitTurnCommand) -> CommandReceipt:
        self.calls.append(command)
        if self.submit_error:
            raise self.submit_error
        if self.active_turn_id is not None:
            raise RuntimeError("session already has an active turn")
        return self._accept(command.session, command.command_id)

    async def resume_turn(self, command: ResumeTurnCommand) -> ResumeTurnResult:
        self.calls.append(command)
        if self.resume_error:
            raise self.resume_error
        receipt = self._accept(command.session, command.command_id, command.expected_turn_id)
        return ResumeTurnResult(receipt.command_id, receipt.session, receipt.turn_id)

    def _accept(
        self, session: SessionRef, command_id: str, turn_id: str | None = None
    ) -> CommandReceipt:
        outcome = (
            self.outcomes.pop(0)
            if self.outcomes
            else FakeOutcome(turn_id=turn_id or "turn-1")
        )
        if self.active_turn_id is not None and self.current_outcome is not None:
            if self.current_outcome.status != "waiting_approval" or turn_id != self.active_turn_id:
                raise RuntimeError("session already has an active turn")
        self.current_outcome = outcome
        self.active_turn_id = outcome.turn_id
        usage = outcome.usage if outcome.usage is not None else self.view.usage
        self.view = SessionView(
            "project", session.thread_id, "running", outcome.turn_id,
            self.view.latest_sequence, usage, None, "",
        )
        self.started.set()
        asyncio.create_task(self._finish(session, outcome))
        return CommandReceipt(command_id, session, outcome.turn_id)

    async def _finish(self, session: SessionRef, outcome: FakeOutcome) -> None:
        await self.release.wait()
        if self.current_outcome is not None and self.current_outcome.turn_id == outcome.turn_id:
            outcome = self.current_outcome
        self.view = SessionView(
            "project", session.thread_id, outcome.status,
            outcome.turn_id if outcome.status == "waiting_approval" else None,
            self.view.latest_sequence, self.view.usage, None, "",
        )
        templates = self.event_templates or [
            RuntimeEvent(
                1, self.view.latest_sequence + 1, outcome.turn_id,
                "turn_" + outcome.status,
                {"final_text": outcome.final_text, "status": outcome.status},
                self.view.latest_sequence + 1,
            )
        ]
        if not any(template.kind == "turn_" + outcome.status for template in templates):
            templates = [
                *templates,
                RuntimeEvent(
                    1, self.view.latest_sequence + len(templates) + 1, outcome.turn_id,
                    "turn_" + outcome.status,
                    {"final_text": outcome.final_text, "status": outcome.status},
                    self.view.latest_sequence + len(templates) + 1,
                ),
            ]
        for template in templates:
            event = RuntimeEvent(
                template.version, self.view.latest_sequence + 1,
                outcome.turn_id if template.turn_id != "other" else "other",
                template.kind, template.payload, self.view.latest_sequence + 1,
            )
            for watcher in tuple(self._watchers):
                watcher.queue.put_nowait(event)
            self.view = SessionView(
                "project", session.thread_id, self.view.status,
                self.view.active_turn_id, event.sequence, self.view.usage, None, "",
            )
        for watcher in tuple(self._watchers):
            watcher.queue.put_nowait(None)
        if outcome.status != "waiting_approval":
            self.active_turn_id = None

    async def cancel_turn(self, command: CancelTurnCommand) -> CancelTurnResult:
        self.calls.append(command)
        if self.active_turn_id == command.expected_turn_id:
            self.current_outcome = FakeOutcome(
                turn_id=command.expected_turn_id, status="cancelled", final_text="",
                usage=self.view.usage,
            )
            self.release.set()
        return CancelTurnResult(command.command_id, command.session, command.expected_turn_id, True)

    async def close_session(self, command: CloseSessionCommand) -> CloseSessionResult:
        self.calls.append(command)
        self.release.set()
        return CloseSessionResult(
            command.command_id, command.session, True,
            self.active_turn_id, command.cancel_active,
        )


def simple_managed(
    descriptor: ACPSessionDescriptor,
    service: FakeAgentRuntimeService | None = None,
    *,
    owner: Any = None,
    copy_session_state: Any = None,
    delete_session_state: Any = None,
) -> ACPManagedSession:
    """Build a service-only managed session for ACP adapter tests."""
    return ACPManagedSession(
        descriptor,
        service or FakeAgentRuntimeService(),
        SessionRef("project", descriptor.thread_id),
        owner,
        copy_session_state,
        delete_session_state,
    )


def managed(
    service: FakeAgentRuntimeService | None = None,
    *, sid: str = "s", owner: Any = None, copy_session_state: Any = None,
    delete_session_state: Any = None,
) -> ACPManagedSession:
    return simple_managed(
        ACPSessionDescriptor(sid, "thread", Path(".")), service,
        owner=owner, copy_session_state=copy_session_state,
        delete_session_state=delete_session_state,
    )
class FakeOwner:
    def __init__(self) -> None:
        self.closes = 0

    async def close(self) -> None:
        self.closes += 1


def event(
    kind: str, turn_id: str = "turn-1", payload: dict[str, Any] | None = None
) -> RuntimeEvent:
    return RuntimeEvent(1, 1, turn_id, kind, payload or {}, 1)


__all__ = ["FakeAgentRuntimeService", "FakeOutcome", "FakeOwner", "event", "managed"]
