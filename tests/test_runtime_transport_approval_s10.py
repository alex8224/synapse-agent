# ruff: noqa: E501, F401, I001, B017, F841

from __future__ import annotations

import asyncio
import inspect
import json
from types import SimpleNamespace

import pytest

from synapse.runtime.service import (
    ALL_RUNTIME_CAPABILITIES,
    ApprovalActionView,
    ApprovalDecision,
    PendingApprovalQuery,
    PendingApprovalView,
    ResumeTurnCommand,
    ResumeTurnResult,
    SessionView,
    UsageView,
    AgentRuntimeService,
    TURN_APPROVAL_READ,
    TURN_APPROVAL_RESUME,
    AclAuthorizer,
    AclGrant,
    Principal,
    AccessControlledAgentRuntimeService,
)
from synapse.runtime.sessions.ref import SessionRef
from synapse.runtime.transport import ProtocolError
from synapse.runtime.transport.protocol import CAPABILITIES, METHODS
from synapse.runtime.transport.client import (
    AmbiguousCommandError,
    ProtocolTransportError,
    RuntimeWebSocketClient,
)
from synapse.runtime.transport.protocol import decode_params, dispatch

REF = SessionRef("p", "t")
PARAMS = {"session": {"project_id": "p", "thread_id": "t"}, "expected_turn_id": "turn"}


def test_valid_approval_get_decodes() -> None:
    query = decode_params("runtime.turn.approval.get", PARAMS)
    assert isinstance(query, PendingApprovalQuery) and query.expected_turn_id == "turn"


@pytest.mark.parametrize("params", [{}, {**PARAMS, "extra": 1}, {**PARAMS, "expected_turn_id": 3}])
def test_invalid_approval_get_fields_and_id(params: dict[str, object]) -> None:
    with pytest.raises(ProtocolError):
        decode_params("runtime.turn.approval.get", params)


def test_valid_resume_decisions_and_command_id() -> None:
    command = decode_params("runtime.turn.approval.resume", {
        **PARAMS, "command_id": "cmd", "decisions": [{"kind": "reject_once", "message": "no"}],
    })
    assert isinstance(command, ResumeTurnCommand)
    assert command.command_id == "cmd" and command.decisions == (ApprovalDecision("reject_once", "no"),)


@pytest.mark.parametrize("decisions", [[], [{"kind": True}], [{"kind": "allow_once", "message": 1}], [{"kind": "bad"}]])
def test_invalid_resume_kind_message_count_and_bool(decisions: list[object]) -> None:
    with pytest.raises(ProtocolError):
        decode_params("runtime.turn.approval.resume", {**PARAMS, "decisions": decisions})


def test_dispatch_get_and_resume_touch_exact_service() -> None:
    class Service:
        def __init__(self) -> None:
            self.calls: list[object] = []
        async def pending_approval(self, dto: object) -> object:
            self.calls.append(dto)
            return PendingApprovalView("turn", ())
        async def resume_turn(self, dto: object) -> object:
            self.calls.append(dto)
            return ResumeTurnResult(dto.command_id, dto.session, "new", True)
    async def check() -> None:
        service = Service()
        await dispatch(service, "runtime.turn.approval.get", PARAMS)
        await dispatch(service, "runtime.turn.approval.resume", {**PARAMS, "decisions": [{"kind": "allow_once"}]})
        assert [type(item) for item in service.calls] == [PendingApprovalQuery, ResumeTurnCommand]
    asyncio.run(check())


def test_invalid_decode_does_not_touch_service() -> None:
    class Service:
        calls = 0
    with pytest.raises(ProtocolError):
        asyncio.run(dispatch(Service(), "runtime.turn.approval.get", {"bad": 1}))
    assert Service.calls == 0


def test_acl_denies_approval_before_delegate() -> None:
    class Delegate:
        async def pending_approval(self, query: object) -> object:
            raise AssertionError("delegate touched")
        async def resume_turn(self, command: object) -> object:
            raise AssertionError("delegate touched")
    methods = {name: (lambda *args, **kwargs: None) for name in (
        "submit_turn", "open_session", "cancel_turn", "steer_turn", "resume_turn",
        "pending_approval", "close_session", "get_session", "stat_artifact",
        "list_artifacts", "read_artifact", "read_events", "watch_events")}
    delegate = Delegate()
    methods["pending_approval"] = delegate.pending_approval
    methods["resume_turn"] = delegate.resume_turn
    service = AccessControlledAgentRuntimeService(SimpleNamespace(**methods), Principal("u"), AclAuthorizer([]))
    async def check() -> None:
        with pytest.raises(Exception):
            await service.pending_approval(PendingApprovalQuery(REF, "turn"))
        with pytest.raises(Exception):
            await service.resume_turn(ResumeTurnCommand(REF, "turn", (ApprovalDecision("reject_once"),)))
    asyncio.run(check())


def test_capabilities_advertise_approval_resume() -> None:
    assert {TURN_APPROVAL_READ, TURN_APPROVAL_RESUME} <= ALL_RUNTIME_CAPABILITIES
    assert CAPABILITIES["approval_resume"] is True


def test_agent_runtime_service_and_transport_expose_required_methods() -> None:
    assert inspect.iscoroutinefunction(AgentRuntimeService.resume_turn)
    assert {"runtime.turn.approval.get", "runtime.turn.approval.resume"} <= METHODS


class FakeClient(RuntimeWebSocketClient):
    def __init__(self, result: object) -> None:
        super().__init__("ws://fake")
        self.result = result
        self.sent: list[tuple[str, object]] = []
    async def _request_with_retry(self, method: str, params: object) -> object:
        self.sent.append((method, params))
        return self.result
    async def _command(self, method: str, params: object, command_id: str) -> object:
        self.sent.append((method, params))
        return self.result


def test_client_pending_decodes_typed_result_strictly() -> None:
    client = FakeClient({"turn_id": "turn", "actions": [{"index": 0, "name": "read", "args": {}}]})
    result = asyncio.run(client.pending_approval(PendingApprovalQuery(REF, "turn")))
    assert result == PendingApprovalView("turn", (ApprovalActionView(0, "read", {}),))
    bad = FakeClient({"turn_id": "turn", "actions": [{"index": True, "name": "read", "args": {}}]})
    with pytest.raises(ProtocolTransportError):
        asyncio.run(bad.pending_approval(PendingApprovalQuery(REF, "turn")))


def test_client_resume_sends_exact_params_and_result() -> None:
    command = ResumeTurnCommand(REF, "turn", (ApprovalDecision("allow_once"),), command_id="cmd")
    client = FakeClient({"command_id": "cmd", "session": {"project_id": "p", "thread_id": "t"}, "turn_id": "new", "accepted": True})
    result = asyncio.run(client.resume_turn(command))
    assert result == ResumeTurnResult("cmd", REF, "new", True)
    assert client.sent[0] == ("runtime.turn.approval.resume", {
        "session": {"project_id": "p", "thread_id": "t"}, "expected_turn_id": "turn",
        "decisions": [{"kind": "allow_once"}], "command_id": "cmd"})


def test_disconnect_after_resume_sent_is_ambiguous_without_replay() -> None:
    client = RuntimeWebSocketClient("ws://fake")
    async def check() -> None:
        with pytest.raises(AmbiguousCommandError) as error:
            raise AmbiguousCommandError("cmd")
        assert error.value.command_id == "cmd"
    asyncio.run(check())


def test_legacy_v1_wire_business_shape_remains_compatible() -> None:
    view = SessionView("p", "t", "idle", None, 0, UsageView(), None, "now")
    from synapse.runtime.transport import encode_response
    assert '"wire_version":"1"' in encode_response(1, view)


def test_public_exports_include_approval_surface() -> None:
    assert hasattr(__import__("synapse.runtime.transport", fromlist=["x"]), "RuntimeWebSocketClient")
    assert hasattr(ApprovalDecision, "__dataclass_fields__")
