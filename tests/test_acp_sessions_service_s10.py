# ruff: noqa: E402, E501, E701, E702, F841, I001

from __future__ import annotations

import ast
import asyncio
from dataclasses import dataclass, fields
from types import SimpleNamespace
from pathlib import Path
from typing import Any

import pytest

from synapse.acp import sessions as module
from synapse.acp.sessions import ACPManagedSession, ACPSessionDescriptor, ACPSessionRegistry
from synapse.runtime.service.commands import (
    ApprovalDecision,
    CancelTurnCommand,
    CloseSessionCommand,
    OpenSessionCommand,
    ResumeTurnCommand,
    SubmitTurnCommand,
)
from synapse.runtime.service.events import RuntimeEvent
from synapse.runtime.sessions.ref import SessionRef


def run(coro: Any) -> Any:
    return asyncio.run(coro)


class Watch:
    def __init__(self, service: FakeService, events: list[RuntimeEvent]) -> None:
        self.service, self.events = service, events

    async def __aenter__(self) -> Watch:
        self.service.calls.append(("watch-enter", self.events))
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self.service.calls.append("watch-exit")

    def __aiter__(self) -> Watch:
        return self

    async def __anext__(self) -> RuntimeEvent:
        if self.events:
            return self.events.pop(0)
        raise StopAsyncIteration


class FakeService:
    def __init__(self, events: list[RuntimeEvent] | None = None) -> None:
        self.calls: list[Any] = []
        self.events = events or []
        self.view = SimpleNamespace(
            latest_sequence=7, active_turn_id="active", status="completed", usage={"in": 1}
        )
        self.receipt = SimpleNamespace(turn_id="t1")
        self.close_error: BaseException | None = None

    async def get_session(self, query: Any) -> Any:
        self.calls.append(query)
        return self.view

    def watch_events(self, session: SessionRef, *, after: int = 0, **kwargs: Any) -> Watch:
        self.calls.append(("watch", session, after))
        return Watch(self, list(self.events))

    async def submit_turn(self, command: SubmitTurnCommand) -> Any:
        self.calls.append(command)
        return self.receipt

    async def resume_turn(self, command: ResumeTurnCommand) -> Any:
        self.calls.append(command)
        return self.receipt

    async def cancel_turn(self, command: CancelTurnCommand) -> Any:
        self.calls.append(command)
        return SimpleNamespace(cancellation_requested=True)

    async def close_session(self, command: CloseSessionCommand) -> Any:
        self.calls.append(command)
        if self.close_error:
            raise self.close_error
        return SimpleNamespace(closed=True)


def event(kind: str, turn_id: str = "t1", payload: dict[str, Any] | None = None) -> RuntimeEvent:
    return RuntimeEvent(1, 8, turn_id, kind, payload or {}, 1)


def managed(service: FakeService | None = None, owner: Any = None, sid: str = "s") -> ACPManagedSession:
    descriptor = ACPSessionDescriptor(sid, "thread", Path("."))
    return ACPManagedSession(descriptor, service or FakeService(), SessionRef("p", "thread"), owner)


@dataclass
class Owner:
    closes: int = 0
    error: BaseException | None = None

    async def close(self) -> None:
        self.closes += 1
        if self.error:
            raise self.error


from dataclasses import dataclass


def test_managed_dataclass_has_only_service_port_state() -> None:
    assert {field.name for field in fields(ACPManagedSession)} == {
        "descriptor", "service", "session", "owner", "copy_session_state", "delete_session_state"
    }


def test_sessions_ast_has_no_runtime_leaks() -> None:
    tree = ast.parse(Path("src/synapse/acp/sessions.py").read_text(encoding="utf-8"))
    source = Path("src/synapse/acp/sessions.py").read_text(encoding="utf-8")
    forbidden = {"RuntimeManager", "SessionRuntime", "TurnHandle", "TurnResult", "UserTurn"}
    assert not forbidden.intersection(node.id for node in ast.walk(tree) if isinstance(node, ast.Name))
    assert not any(
        isinstance(node, ast.Attribute) and node.attr in {"manager", "runtime"}
        for node in ast.walk(tree)
    )


def test_submit_watches_before_submit_with_exact_command() -> None:
    service = FakeService([event("turn_completed", payload={"final_text": "ok"})])
    result = run(managed(service).submit("hello", ("a",)))
    assert result.final_text == "ok"
    assert service.calls[2][0] == "watch-enter"
    command = service.calls[3]
    assert isinstance(command, SubmitTurnCommand)
    assert (command.session, command.text, command.attachments) == (SessionRef("p", "thread"), "hello", ("a",))


def test_submit_outcome_contains_turn_status_final_and_usage() -> None:
    service = FakeService([event("answer_completed", payload={"text": "final"}), event("usage_updated", payload={"out": 2}), event("turn_completed")])
    assert run(managed(service).submit("x")) == module.ACPTurnOutcome("t1", "completed", "final", {"out": 2})


def test_submit_ignores_other_turn_and_sync_callback() -> None:
    seen: list[str] = []
    service = FakeService([event("turn_completed", "other"), event("turn_completed")])
    assert run(managed(service).submit("x", on_event=lambda item: seen.append(item.turn_id))).turn_id == "t1"
    assert seen == ["t1"]


def test_submit_supports_async_callback() -> None:
    seen: list[str] = []
    async def callback(item: RuntimeEvent) -> None:
        seen.append(item.kind)
    run(managed(FakeService([event("turn_completed")])).submit("x", on_event=callback))
    assert seen == ["turn_completed"]


def test_resume_watches_before_exact_command() -> None:
    decisions = (ApprovalDecision("allow_once"),)
    service = FakeService([event("turn_completed")])
    run(managed(service).resume("expected", decisions))
    assert service.calls[2][0] == "watch-enter"
    command = service.calls[3]
    assert isinstance(command, ResumeTurnCommand)
    assert (command.session, command.expected_turn_id, command.decisions) == (SessionRef("p", "thread"), "expected", decisions)


def test_resume_returns_outcome() -> None:
    service = FakeService([event("turn_waiting_approval")])
    assert run(managed(service).resume("expected", (ApprovalDecision("reject_once", "no"),))).status == "waiting_approval"


def test_cancel_active_is_fenced() -> None:
    service = FakeService()
    assert run(managed(service).cancel("stop")) is True
    command = service.calls[-1]
    assert isinstance(command, CancelTurnCommand)
    assert (command.expected_turn_id, command.reason) == ("active", "stop")


def test_cancel_without_active_returns_false_without_command() -> None:
    service = FakeService(); service.view.active_turn_id = None
    assert run(managed(service).cancel()) is False
    assert not any(isinstance(call, CancelTurnCommand) for call in service.calls)


def test_close_sends_exact_command() -> None:
    service = FakeService(); run(managed(service).close(True))
    assert service.calls[-1] == CloseSessionCommand(SessionRef("p", "thread"), True, service.calls[-1].command_id)
    assert service.calls[-1].cancel_active is True


def test_registry_create_add_duplicate_and_factory_cleanup() -> None:
    owners: list[Owner] = []
    async def factory(desc: ACPSessionDescriptor) -> ACPManagedSession:
        owner = Owner(); owners.append(owner)
        return managed(owner=owner, sid=desc.session_id)
    async def check() -> None:
        registry = ACPSessionRegistry(factory)
        await registry.create(cwd=Path("."), session_id="same")
        with pytest.raises(RuntimeError):
            await registry.create(cwd=Path("."), session_id="same")
        assert owners[-1].closes == 1
        with pytest.raises(RuntimeError):
            await registry.add(managed(owner=owners[0], sid="same"))
    run(check())


def test_registry_close_missing_is_false() -> None:
    assert run(ACPSessionRegistry(lambda d: None).close("missing")) is False


def test_registry_close_normal_closes_last_owner() -> None:
    owner = Owner(); registry = ACPSessionRegistry(lambda d: None)
    run(registry.add(managed(owner=owner)))
    assert run(registry.close("s")) is True and owner.closes == 1


def test_managed_close_failure_keeps_last_owner_cleanup() -> None:
    owner = Owner(); item = managed(owner=owner); item.service.close_error = ValueError("x")
    registry = ACPSessionRegistry(lambda d: None); run(registry.add(item))
    with pytest.raises(ValueError): run(registry.close("s"))
    assert owner.closes == 1


def test_shared_owner_closes_only_after_last_session() -> None:
    owner = Owner(); registry = ACPSessionRegistry(lambda d: None)
    run(registry.add(managed(owner=owner, sid="a"))); run(registry.add(managed(owner=owner, sid="b")))
    run(registry.close("a")); assert owner.closes == 0
    run(registry.close("b")); assert owner.closes == 1


def test_shutdown_closes_sessions_and_deduplicates_owners() -> None:
    owner = Owner(); registry = ACPSessionRegistry(lambda d: None)
    run(registry.add(managed(owner=owner, sid="a"))); run(registry.add(managed(owner=owner, sid="b")))
    run(registry.shutdown()); assert owner.closes == 1 and len(registry) == 0


def test_shutdown_continues_siblings_and_raises_first_error() -> None:
    first = managed(sid="a"); first.service.close_error = ValueError("first")
    second = managed(sid="b"); registry = ACPSessionRegistry(lambda d: None)
    run(registry.add(first)); run(registry.add(second))
    with pytest.raises(ValueError, match="first"): run(registry.shutdown())
    assert any(isinstance(c, CloseSessionCommand) for c in second.service.calls)


def test_repeated_concurrent_shutdown_has_one_cleanup() -> None:
    owner = Owner(); registry = ACPSessionRegistry(lambda d: None); run(registry.add(managed(owner=owner)))
    async def check() -> None:
        await asyncio.gather(registry.shutdown(), registry.shutdown())
    run(check())
    assert owner.closes == 1


def test_cancelled_shutdown_is_shielded() -> None:
    owner = Owner(); registry = ACPSessionRegistry(lambda d: None); run(registry.add(managed(owner=owner)))
    async def check() -> None:
        task = asyncio.create_task(registry.shutdown()); await asyncio.sleep(0); task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        await registry.shutdown()
    run(check()); assert owner.closes == 1


def test_factory_uses_project_identity_and_open_command(monkeypatch: pytest.MonkeyPatch) -> None:
    class Settings: pass
    owner = Owner(); service = FakeService()
    class Consumer:
        def __init__(self, **kwargs: Any) -> None:
            self.service = service; self.owner = owner
        async def close(self) -> None:
            await owner.close()
    async def open_session(command: Any) -> Any:
        service.calls.append(command)
        return SimpleNamespace(view=service.view)
    service.open_session = open_session  # type: ignore[method-assign]
    monkeypatch.setattr(module, "LocalProjectRuntimeConsumer", Consumer)
    monkeypatch.setattr(module, "project_identity_for_workspace", lambda settings, cwd: ("project", None))
    factory = module.make_runtime_session_factory(settings_factory=lambda cwd: Settings(), agent_factory=lambda s, d: object())
    result = run(factory(ACPSessionDescriptor("s", "thread", Path("."))))
    assert result.session == SessionRef("project", "thread")
    assert isinstance(service.calls[-1], OpenSessionCommand)


def test_factory_open_failure_rolls_owner_back(monkeypatch: pytest.MonkeyPatch) -> None:
    owner = Owner(); service = FakeService(); service.close_error = None
    class Consumer:
        def __init__(self, **kwargs: Any) -> None: self.service = service; self.owner = owner
        async def close(self) -> None: await owner.close()
    async def fail(command: Any) -> Any: raise RuntimeError("open")
    service.open_session = fail  # type: ignore[method-assign]
    monkeypatch.setattr(module, "LocalProjectRuntimeConsumer", Consumer)
    monkeypatch.setattr(module, "project_identity_for_workspace", lambda settings, cwd: ("p", None))
    factory = module.make_runtime_session_factory(settings_factory=lambda cwd: object(), agent_factory=lambda s, d: object())
    with pytest.raises(RuntimeError, match="open"): run(factory(ACPSessionDescriptor("s", "t", Path("."))))
    assert owner.closes == 1
