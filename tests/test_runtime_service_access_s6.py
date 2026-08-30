"""S6 in-process ACL contracts and application-port wrapper tests."""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import inspect
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from synapse.runtime.service import (
    ALL_RUNTIME_CAPABILITIES,
    ARTIFACTS_LIST,
    ARTIFACTS_READ,
    ARTIFACTS_STAT,
    EVENTS_READ,
    EVENTS_WATCH,
    SESSION_CLOSE,
    SESSION_OPEN,
    SESSION_READ,
    TURN_APPROVAL_READ,
    TURN_APPROVAL_RESUME,
    TURN_CANCEL,
    TURN_STEER,
    TURN_SUBMIT,
    AccessControlledAgentRuntimeService,
    AccessRequest,
    AclAuthorizer,
    AclGrant,
    ApprovalDecision,
    CancelTurnCommand,
    CloseSessionCommand,
    GetSessionQuery,
    InvalidAccessContextError,
    InvalidRequestError,
    ListArtifactsQuery,
    OpenSessionCommand,
    PendingApprovalQuery,
    PermissionDeniedError,
    Principal,
    ReadArtifactQuery,
    ReadEventsQuery,
    ResumeTurnCommand,
    StatArtifactQuery,
    SteerTurnCommand,
    SubmitTurnCommand,
    bind_access,
)
from synapse.runtime.service.artifacts import ArtifactRef
from synapse.runtime.service.errors import (
    ArtifactNotFoundError,
    ClosedError,
    ConflictError,
    NotFoundError,
    RuntimeServiceError,
)
from synapse.runtime.service.events import EventFilter
from synapse.runtime.service.local import LocalAgentRuntimeService
from synapse.runtime.sessions import RuntimeManager
from synapse.runtime.sessions.ref import SessionRef
from synapse.runtime.streaming import (
    EVENT_VERSION,
    TextPayload,
    TurnEvent,
    TurnEventKind,
)

REF = SessionRef("project-a", "thread-a")
OTHER_PROJECT = SessionRef("project-b", "thread-a")
PRINCIPAL = Principal("subject-a")


class SpyDelegate:
    def __init__(self, *, error: BaseException | None = None) -> None:
        self.calls: list[str] = []
        self.error = error
        self.watch_lease = object()

    async def _call(self, name: str) -> Any:
        self.calls.append(name)
        if self.error is not None:
            raise self.error
        return name

    async def submit_turn(self, command: SubmitTurnCommand) -> Any:
        return await self._call("submit")

    async def open_session(self, command: OpenSessionCommand) -> Any:
        return await self._call("open")

    async def cancel_turn(self, command: CancelTurnCommand) -> Any:
        return await self._call("cancel")

    async def steer_turn(self, command: SteerTurnCommand) -> Any:
        return await self._call("steer")

    async def close_session(self, command: CloseSessionCommand) -> Any:
        return await self._call("close")

    async def get_session(self, query: GetSessionQuery) -> Any:
        return await self._call("get")

    async def stat_artifact(self, query: StatArtifactQuery) -> Any:
        return await self._call("stat")

    async def list_artifacts(self, query: ListArtifactsQuery) -> Any:
        return await self._call("list")

    async def read_artifact(self, query: ReadArtifactQuery) -> Any:
        return await self._call("read_artifact")

    async def read_events(self, query: ReadEventsQuery) -> Any:
        return await self._call("read_events")

    async def pending_approval(self, query: PendingApprovalQuery) -> Any:
        return await self._call("pending_approval")

    async def resume_turn(self, command: ResumeTurnCommand) -> Any:
        return await self._call("resume")

    def watch_events(self, session: SessionRef, **kwargs: Any) -> Any:
        self.calls.append("watch")
        if self.error is not None:
            raise self.error
        return self.watch_lease


def _authorizer(*capabilities: str, thread_ids: frozenset[str] | None = None) -> AclAuthorizer:
    return AclAuthorizer(
        [AclGrant(PRINCIPAL.subject, REF.project_id, frozenset(capabilities), thread_ids)]
    )


def test_access_dtos_are_frozen_slotted_copy_isolated_and_json_projectable() -> None:
    capabilities = {SESSION_OPEN}
    threads = {REF.thread_id}
    grant = AclGrant(PRINCIPAL.subject, REF.project_id, capabilities, threads)
    capabilities.add(TURN_SUBMIT)
    threads.add("later")

    for dto in (PRINCIPAL, grant, AccessRequest(PRINCIPAL, SESSION_OPEN, REF)):
        assert dataclasses.is_dataclass(dto)
        assert type(dto).__dataclass_params__.frozen is True
        assert hasattr(type(dto), "__slots__")
    assert grant.capabilities == frozenset({SESSION_OPEN})
    assert grant.thread_ids == frozenset({REF.thread_id})
    assert json.loads(json.dumps(dataclasses.asdict(PRINCIPAL))) == {"subject": "subject-a"}
    assert isinstance(dataclasses.asdict(grant)["capabilities"], frozenset)

    with pytest.raises(ValueError):
        Principal("secret\x00subject")
    with pytest.raises(ValueError):
        Principal("é" * 129)
    for bad in (True, 1, "", "bad\x00value", object()):
        with pytest.raises(ValueError):
            Principal(bad)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unknown capability"):
        AclGrant("s", "p", frozenset({"not-a-capability"}))
    with pytest.raises(ValueError):
        AclGrant("s", "p", frozenset(), frozenset())


def test_capabilities_and_error_codes_are_stable_and_exported() -> None:
    assert ALL_RUNTIME_CAPABILITIES == frozenset(
        {
            SESSION_OPEN,
            TURN_SUBMIT,
            TURN_CANCEL,
            TURN_STEER,
            SESSION_CLOSE,
            SESSION_READ,
            EVENTS_READ,
            EVENTS_WATCH,
            ARTIFACTS_STAT,
            ARTIFACTS_LIST,
            ARTIFACTS_READ,
            TURN_APPROVAL_READ,
            TURN_APPROVAL_RESUME,
        }
    )
    assert PermissionDeniedError().code == "permission_denied"
    assert PermissionDeniedError("secret", code="not_found").code == "permission_denied"
    assert InvalidAccessContextError("safe").code == "invalid_access_context"
    assert isinstance(PermissionDeniedError(), RuntimeServiceError)
    assert str(PermissionDeniedError("secret")) == "operation is not permitted"
    assert all(
        inspect.iscoroutinefunction(getattr(AccessControlledAgentRuntimeService, name))
        for name in (
            "submit_turn",
            "open_session",
            "cancel_turn",
            "steer_turn",
            "close_session",
            "get_session",
            "stat_artifact",
            "list_artifacts",
            "read_artifact",
            "read_events",
            "pending_approval",
            "resume_turn",
        )
    )
    assert isinstance(
        bind_access(SpyDelegate(), PRINCIPAL, AclAuthorizer([])),
        AccessControlledAgentRuntimeService,
    )


def test_authorizer_is_exact_scope_default_deny_and_thread_safe() -> None:
    authorizer = AclAuthorizer(
        [
            AclGrant("subject-a", "project-a", frozenset({SESSION_READ}), frozenset({"thread-a"})),
            AclGrant("subject-a", "project-a", frozenset({TURN_SUBMIT}), frozenset({"thread-b"})),
            AclGrant("subject-a", "project-a", frozenset({EVENTS_READ})),
        ]
    )
    authorizer.authorize(PRINCIPAL, SESSION_READ, REF)
    authorizer.authorize(PRINCIPAL, EVENTS_READ, SessionRef("project-a", "unknown"))
    with pytest.raises(PermissionDeniedError):
        authorizer.authorize(PRINCIPAL, SESSION_READ, SessionRef("project-a", "thread-b"))
    for principal, session, capability in (
        (Principal("subject-b"), REF, SESSION_READ),
        (PRINCIPAL, OTHER_PROJECT, EVENTS_READ),
        (PRINCIPAL, REF, "session.open"),
    ):
        if capability == "session.open":
            with pytest.raises(PermissionDeniedError):
                authorizer.authorize(principal, capability, session)
        else:
            with pytest.raises(PermissionDeniedError):
                authorizer.authorize(principal, capability, session)
    with pytest.raises(ValueError):
        authorizer.authorize(PRINCIPAL, "events.*", REF)
    with pytest.raises(InvalidAccessContextError):
        authorizer.authorize(object(), SESSION_READ, REF)
    with pytest.raises(InvalidAccessContextError):
        authorizer.authorize(PRINCIPAL, SESSION_READ, object())

    def check() -> bool:
        try:
            authorizer.authorize(PRINCIPAL, EVENTS_READ, REF)
            return True
        except RuntimeServiceError:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert all(pool.map(lambda _: check(), range(100)))
    with pytest.raises(AttributeError):
        authorizer._grants = ()  # type: ignore[misc]


@pytest.mark.parametrize(
    ("method", "command", "capability"),
    [
        ("submit_turn", SubmitTurnCommand(REF, "text"), TURN_SUBMIT),
        ("open_session", OpenSessionCommand(REF), SESSION_OPEN),
        ("cancel_turn", CancelTurnCommand(REF, "turn"), TURN_CANCEL),
        ("steer_turn", SteerTurnCommand(REF, "turn", "text"), TURN_STEER),
        ("close_session", CloseSessionCommand(REF), SESSION_CLOSE),
        ("get_session", GetSessionQuery(REF), SESSION_READ),
        ("stat_artifact", StatArtifactQuery(ArtifactRef(REF, "file")), ARTIFACTS_STAT),
        ("list_artifacts", ListArtifactsQuery(REF), ARTIFACTS_LIST),
        ("read_artifact", ReadArtifactQuery(ArtifactRef(REF, "file")), ARTIFACTS_READ),
        ("read_events", ReadEventsQuery(REF), EVENTS_READ),
        ("pending_approval", PendingApprovalQuery(REF, "turn"), TURN_APPROVAL_READ),
        (
            "resume_turn",
            ResumeTurnCommand(REF, "turn", (ApprovalDecision("allow_once"),)),
            TURN_APPROVAL_RESUME,
        ),
    ],
)
def test_each_async_port_authorizes_before_delegate(
    method: str, command: Any, capability: str
) -> None:
    async def run() -> None:
        delegate = SpyDelegate()
        wrapper = bind_access(delegate, PRINCIPAL, _authorizer(capability))
        expected_call = {
            "submit_turn": "submit",
            "open_session": "open",
            "cancel_turn": "cancel",
            "steer_turn": "steer",
            "close_session": "close",
            "get_session": "get",
            "stat_artifact": "stat",
            "list_artifacts": "list",
            "read_artifact": "read_artifact",
            "read_events": "read_events",
            "pending_approval": "pending_approval",
            "resume_turn": "resume",
        }.get(method, method)
        assert await getattr(wrapper, method)(command) == expected_call
        assert delegate.calls == [expected_call]

        denied_delegate = SpyDelegate()
        denied = bind_access(denied_delegate, PRINCIPAL, AclAuthorizer([]))
        with pytest.raises(PermissionDeniedError) as exc:
            await getattr(denied, method)(command)
        assert exc.value.code == "permission_denied"
        assert str(exc.value) == "operation is not permitted"
        assert denied_delegate.calls == []

    asyncio.run(run())


@pytest.mark.parametrize("missing", ["pending_approval", "resume_turn"])
def test_constructor_rejects_delegate_missing_each_approval_method(missing: str) -> None:
    methods = {
        name: value
        for name, value in SpyDelegate.__dict__.items()
        if name != missing
    }
    IncompleteDelegate = type("IncompleteDelegate", (object,), methods)

    with pytest.raises(TypeError):
        bind_access(IncompleteDelegate(), PRINCIPAL, AclAuthorizer([]))  # type: ignore[arg-type]


def test_constructor_rejects_dynamic_getattr_delegate() -> None:
    class DynamicDelegate:
        def __getattr__(self, name: str) -> Any:
            return lambda *args, **kwargs: None

    with pytest.raises(TypeError):
        bind_access(DynamicDelegate(), PRINCIPAL, AclAuthorizer([]))


@pytest.mark.parametrize("session", [None, object()])
def test_watch_malformed_session_is_invalid_request_before_authorization(
    session: object,
) -> None:
    async def run() -> None:
        delegate = SpyDelegate()
        wrapper = bind_access(delegate, PRINCIPAL, AclAuthorizer([]))
        with pytest.raises(InvalidRequestError) as caught:
            wrapper.watch_events(session)  # type: ignore[arg-type]
        assert "permission_denied" not in str(caught.value)
        assert delegate.calls == []

    asyncio.run(run())


@pytest.mark.parametrize(
    "error",
    [
        NotFoundError("not found"),
        ConflictError("conflict"),
        ClosedError("closed"),
        ArtifactNotFoundError("artifact missing"),
        RuntimeError("ordinary"),
        asyncio.CancelledError(),
        KeyboardInterrupt(),
    ],
)
def test_allowed_delegate_errors_propagate_unchanged(error: BaseException) -> None:
    async def run() -> None:
        delegate = SpyDelegate(error=error)
        wrapper = bind_access(delegate, PRINCIPAL, _authorizer(SESSION_READ))
        with pytest.raises(type(error)) as caught:
            await wrapper.get_session(GetSessionQuery(REF))
        assert caught.value is error

    asyncio.run(run())


def test_constructor_rejects_invalid_context_and_delegate_without_repr() -> None:
    class Secret:
        def __repr__(self) -> str:
            raise AssertionError("malicious repr executed")

    for principal in (object(), object.__new__(Principal)):
        with pytest.raises(TypeError) as caught:
            bind_access(SpyDelegate(), principal, AclAuthorizer([]))  # type: ignore[arg-type]
        assert "malicious repr" not in str(caught.value)
    for authorizer in (object(), object.__new__(AclAuthorizer)):
        with pytest.raises(TypeError) as caught:
            bind_access(SpyDelegate(), PRINCIPAL, authorizer)  # type: ignore[arg-type]
        assert "malicious repr" not in str(caught.value)
    for delegate in (object(), Secret()):
        with pytest.raises(TypeError) as caught:
            bind_access(delegate, PRINCIPAL, AclAuthorizer([]))  # type: ignore[arg-type]
        assert "malicious repr" not in str(caught.value)


def test_access_request_malformed_context_is_safe_value_error() -> None:
    class Secret:
        def __repr__(self) -> str:
            raise AssertionError("malicious repr executed")

    for principal, capability, session in (
        (Secret(), SESSION_READ, REF),
        (PRINCIPAL, Secret(), REF),
        (PRINCIPAL, SESSION_READ, Secret()),
    ):
        with pytest.raises(ValueError) as caught:
            AccessRequest(principal, capability, session)  # type: ignore[arg-type]
        assert "malicious repr" not in str(caught.value)
        assert "secret-value" not in str(caught.value)


def test_real_local_service_acl_open_read_artifact_and_close(tmp_path: Any) -> None:
    async def run() -> None:
        (tmp_path / "visible.txt").write_text("hello", encoding="utf-8")
        settings = SimpleNamespace(
            workspace=tmp_path,
            deny_fs_paths=[],
            max_concurrency=2,
            model="test",
        )
        manager = RuntimeManager(
            settings=settings,
            agent_factory=lambda thread_id, shared: SimpleNamespace(thread_id=thread_id),
            project_id=REF.project_id,
        )
        service = LocalAgentRuntimeService(
            lambda project: manager if project == REF.project_id else None
        )
        allowed = bind_access(
            service,
            PRINCIPAL,
            _authorizer(SESSION_OPEN, SESSION_READ, ARTIFACTS_READ, ARTIFACTS_STAT),
        )
        await allowed.open_session(OpenSessionCommand(REF))
        before = await allowed.get_session(GetSessionQuery(REF))
        chunk = await allowed.read_artifact(ReadArtifactQuery(ArtifactRef(REF, "visible.txt")))
        assert chunk.byte_length == 5
        assert (
            await allowed.stat_artifact(StatArtifactQuery(ArtifactRef(REF, "visible.txt")))
        ).kind == "file"
        with pytest.raises(PermissionDeniedError):
            await allowed.list_artifacts(ListArtifactsQuery(REF))
        listed = bind_access(service, PRINCIPAL, _authorizer(ARTIFACTS_LIST))
        page = await listed.list_artifacts(ListArtifactsQuery(REF))
        assert [entry.path for entry in page.entries] == ["visible.txt"]
        with pytest.raises(PermissionDeniedError):
            await allowed.submit_turn(SubmitTurnCommand(REF, "must deny"))
        after = await allowed.get_session(GetSessionQuery(REF))
        assert (after.latest_sequence, after.status) == (before.latest_sequence, before.status)

        no_close = bind_access(service, PRINCIPAL, _authorizer(SESSION_READ))
        with pytest.raises(PermissionDeniedError):
            await no_close.close_session(CloseSessionCommand(REF))
        assert (await no_close.get_session(GetSessionQuery(REF))).status == "idle"
        can_close = bind_access(service, PRINCIPAL, _authorizer(SESSION_CLOSE))
        await can_close.close_session(CloseSessionCommand(REF))
        assert manager.get_session_ref(REF) is None

    asyncio.run(run())


def test_real_watch_replays_and_detaches_without_closing_session() -> None:
    async def run() -> None:
        settings = SimpleNamespace(
            workspace=None, deny_fs_paths=[], max_concurrency=2, model="test"
        )
        manager = RuntimeManager(
            settings=settings,
            agent_factory=lambda thread_id, shared: SimpleNamespace(thread_id=thread_id),
            project_id=REF.project_id,
        )
        service = LocalAgentRuntimeService(
            lambda project: manager if project == REF.project_id else None
        )
        await service.open_session(OpenSessionCommand(REF))
        session = manager.get_session_ref(REF)
        assert session is not None
        session.broker.emit(
            TurnEvent(
                version=EVENT_VERSION,
                thread_id=REF.thread_id,
                turn_id="turn",
                sequence=1,
                kind=TurnEventKind.ANSWER_DELTA,
                payload=TextPayload("replay"),
            )
        )
        allowed = bind_access(service, PRINCIPAL, _authorizer(EVENTS_WATCH, SESSION_READ))
        async with allowed.watch_events(REF) as stream:
            event = await stream.__anext__()
            assert event.payload["text"] == "replay"
            session.broker.emit(
                TurnEvent(
                    version=EVENT_VERSION,
                    thread_id=REF.thread_id,
                    turn_id="turn",
                    sequence=2,
                    kind=TurnEventKind.ANSWER_DELTA,
                    payload=TextPayload("live"),
                )
            )
            assert (await stream.__anext__()).payload["text"] == "live"
        unentered = allowed.watch_events(REF)
        assert not unentered.closed
        assert manager.get_session_ref(REF) is session
        denied = bind_access(service, PRINCIPAL, AclAuthorizer([]))
        subscriber_count = len(session.broker._subscribers)  # type: ignore[attr-defined]
        with pytest.raises(PermissionDeniedError):
            denied.watch_events(REF)
        assert len(session.broker._subscribers) == subscriber_count  # type: ignore[attr-defined]

    asyncio.run(run())


def test_access_module_import_guard_is_independent_of_application_layers() -> None:
    source = Path(__file__).parents[1] / "src" / "synapse" / "runtime" / "service" / "access.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    imports.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    forbidden = (
        "settings",
        "projects",
        "acp",
        "ui",
        "transport",
        "deepagents",
        "runtime.safety",
        "fs_permissions",
    )
    assert not any(
        any(
            component == blocked or component.endswith(f".{blocked}")
            for blocked in forbidden
            for component in module.split(".")
        )
        for module in imports
    )


@pytest.mark.parametrize(
    ("method", "port_value", "capability"),
    [
        ("submit_turn", SubmitTurnCommand(REF, "text"), TURN_SUBMIT),
        ("open_session", OpenSessionCommand(REF), SESSION_OPEN),
        ("cancel_turn", CancelTurnCommand(REF, "turn"), TURN_CANCEL),
        ("steer_turn", SteerTurnCommand(REF, "turn", "text"), TURN_STEER),
        ("close_session", CloseSessionCommand(REF), SESSION_CLOSE),
        ("get_session", GetSessionQuery(REF), SESSION_READ),
        ("stat_artifact", StatArtifactQuery(ArtifactRef(REF, "file")), ARTIFACTS_STAT),
        ("list_artifacts", ListArtifactsQuery(REF), ARTIFACTS_LIST),
        ("read_artifact", ReadArtifactQuery(ArtifactRef(REF, "file")), ARTIFACTS_READ),
        ("read_events", ReadEventsQuery(REF), EVENTS_READ),
        ("watch_events", REF, EVENTS_WATCH),
    ],
)
def test_all_thirteen_ports_have_complete_capability_separation(
    method: str, port_value: Any, capability: str
) -> None:
    async def run() -> None:
        other = next(iter(ALL_RUNTIME_CAPABILITIES - {capability}))
        denied_delegate = SpyDelegate()
        denied = bind_access(denied_delegate, PRINCIPAL, _authorizer(other))
        with pytest.raises(PermissionDeniedError):
            if method == "watch_events":
                denied.watch_events(port_value)
            else:
                await getattr(denied, method)(port_value)
        assert denied_delegate.calls == []

        allowed_delegate = SpyDelegate()
        allowed = bind_access(allowed_delegate, PRINCIPAL, _authorizer(capability))
        if method == "watch_events":
            assert allowed.watch_events(port_value) is allowed_delegate.watch_lease
            expected = "watch"
        else:
            expected = {
                "submit_turn": "submit",
                "open_session": "open",
                "cancel_turn": "cancel",
                "steer_turn": "steer",
                "close_session": "close",
                "get_session": "get",
                "stat_artifact": "stat",
                "list_artifacts": "list",
                "read_artifact": "read_artifact",
                "read_events": "read_events",
                "pending_approval": "pending_approval",
                "resume_turn": "resume",
            }[method]
            await getattr(allowed, method)(port_value)
        assert allowed_delegate.calls == [expected]

    asyncio.run(run())


@pytest.mark.parametrize(
    "port_value",
    [
        GetSessionQuery(SessionRef("unknown-project", "thread-a")),
        GetSessionQuery(SessionRef("project-a", "unknown-session")),
        ReadEventsQuery(SessionRef("unknown-project", "secret-thread")),
        StatArtifactQuery(ArtifactRef(REF, "secret/artifact")),
        ReadArtifactQuery(ArtifactRef(REF, "secret/artifact")),
        PendingApprovalQuery(SessionRef("unknown-project", "secret-thread"), "turn"),
        ResumeTurnCommand(REF, "secret-turn", (ApprovalDecision("reject_once"),)),
    ],
)
def test_denials_are_non_oracular_and_do_not_touch_delegate(port_value: Any) -> None:
    async def run() -> None:
        delegate = SpyDelegate()
        wrapper = bind_access(delegate, PRINCIPAL, AclAuthorizer([]))
        with pytest.raises(PermissionDeniedError) as caught:
            if isinstance(port_value, GetSessionQuery):
                await wrapper.get_session(port_value)
            elif isinstance(port_value, ReadEventsQuery):
                await wrapper.read_events(port_value)
            elif isinstance(port_value, PendingApprovalQuery):
                await wrapper.pending_approval(port_value)
            elif isinstance(port_value, ResumeTurnCommand):
                await wrapper.resume_turn(port_value)
            elif isinstance(port_value, StatArtifactQuery):
                await wrapper.stat_artifact(port_value)
            else:
                await wrapper.read_artifact(port_value)
        assert (caught.value.code, str(caught.value)) == (
            "permission_denied",
            "operation is not permitted",
        )
        assert delegate.calls == []
        for secret in ("unknown-project", "unknown-session", "secret-thread", "secret/artifact"):
            assert secret not in str(caught.value)

    asyncio.run(run())


@pytest.mark.parametrize(
    "scope",
    [
        ("subject-a", "project-a", "thread-a"),
        ("Subject-a", "project-a", "thread-a"),
        ("subject-a-prefix", "project-a", "thread-a"),
        ("subject-a", "Project-a", "thread-a"),
        ("subject-a", "project-a-prefix", "thread-a"),
        ("subject-a", "project-a", "Thread-a"),
        ("subject-a", "project-a", "thread-a-prefix"),
    ],
)
def test_acl_scope_is_exact_without_case_or_prefix_matching(
    scope: tuple[str, str, str]
) -> None:
    subject, project, thread = scope
    grant = AclGrant(subject, project, frozenset({SESSION_READ}), frozenset({thread}))
    authorizer = AclAuthorizer([grant])
    if scope == ("subject-a", "project-a", "thread-a"):
        authorizer.authorize(PRINCIPAL, SESSION_READ, REF)
    else:
        with pytest.raises(PermissionDeniedError):
            authorizer.authorize(PRINCIPAL, SESSION_READ, REF)


def test_acl_scope_project_wide_session_scope_cross_project_and_or_semantics() -> None:
    authorizer = AclAuthorizer(
        [
            AclGrant("subject-a", "project-a", frozenset({SESSION_READ})),
            AclGrant("subject-a", "project-a", frozenset({EVENTS_READ}), frozenset({"thread-a"})),
        ]
    )
    authorizer.authorize(PRINCIPAL, SESSION_READ, SessionRef("project-a", "unknown-valid-thread"))
    authorizer.authorize(PRINCIPAL, EVENTS_READ, REF)
    with pytest.raises(PermissionDeniedError):
        authorizer.authorize(PRINCIPAL, EVENTS_READ, SessionRef("project-a", "thread-b"))
    with pytest.raises(PermissionDeniedError):
        authorizer.authorize(PRINCIPAL, SESSION_READ, OTHER_PROJECT)
    with pytest.raises(PermissionDeniedError):
        authorizer.authorize(Principal("subject-b"), SESSION_READ, REF)


@pytest.mark.parametrize(
    ("method", "valid"),
    [
        ("submit_turn", SubmitTurnCommand(REF, "text")),
        ("open_session", OpenSessionCommand(REF)),
        ("cancel_turn", CancelTurnCommand(REF, "turn")),
        ("steer_turn", SteerTurnCommand(REF, "turn", "text")),
        ("close_session", CloseSessionCommand(REF)),
        ("get_session", GetSessionQuery(REF)),
        ("stat_artifact", StatArtifactQuery(ArtifactRef(REF, "file"))),
        ("list_artifacts", ListArtifactsQuery(REF)),
        ("read_artifact", ReadArtifactQuery(ArtifactRef(REF, "file"))),
        ("read_events", ReadEventsQuery(REF)),
        ("pending_approval", PendingApprovalQuery(REF, "turn")),
        ("resume_turn", ResumeTurnCommand(REF, "turn", (ApprovalDecision("allow_once"),))),
    ],
)
def test_every_async_entrypoint_rejects_bad_top_level_and_session_without_authorizing(
    method: str, valid: Any
) -> None:
    class Secret:
        def __repr__(self) -> str:
            raise AssertionError("malicious repr executed")

    async def run() -> None:
        delegate = SpyDelegate()
        wrapper = bind_access(delegate, PRINCIPAL, AclAuthorizer([]))
        malformed = object.__new__(type(valid))
        if hasattr(valid, "session"):
            object.__setattr__(malformed, "session", Secret())
        else:
            object.__setattr__(malformed, "ref", Secret())
        for value in (Secret(), malformed):
            with pytest.raises(InvalidRequestError) as caught:
                await getattr(wrapper, method)(value)
            assert "malicious repr" not in str(caught.value)
            assert "secret-value" not in str(caught.value)
        malformed_none = object.__new__(type(valid))
        if hasattr(valid, "session"):
            object.__setattr__(malformed_none, "session", None)
        else:
            object.__setattr__(malformed_none, "ref", None)
        with pytest.raises(InvalidRequestError):
            await getattr(wrapper, method)(malformed_none)
        assert delegate.calls == []

    asyncio.run(run())


def test_malformed_artifact_refs_are_invalid_request_before_authorization() -> None:
    class Secret:
        def __repr__(self) -> str:
            raise AssertionError("malicious repr executed")

    async def run() -> None:
        delegate = SpyDelegate()
        wrapper = bind_access(delegate, PRINCIPAL, AclAuthorizer([]))
        for method, query_type in (
            ("stat_artifact", StatArtifactQuery),
            ("read_artifact", ReadArtifactQuery),
        ):
            query = object.__new__(query_type)
            ref = object.__new__(ArtifactRef)
            object.__setattr__(ref, "session", Secret())
            object.__setattr__(ref, "path", "secret/path")
            object.__setattr__(query, "ref", ref)
            with pytest.raises(InvalidRequestError) as caught:
                await getattr(wrapper, method)(query)
            assert "malicious repr" not in str(caught.value)
            assert "secret/path" not in str(caught.value)
        assert delegate.calls == []

    asyncio.run(run())


def test_artifact_and_watch_calls_use_independent_capabilities() -> None:
    async def run() -> None:
        delegate = SpyDelegate()
        wrapper = bind_access(delegate, PRINCIPAL, _authorizer(ARTIFACTS_STAT))
        with pytest.raises(PermissionDeniedError):
            await wrapper.list_artifacts(ListArtifactsQuery(REF))
        with pytest.raises(PermissionDeniedError):
            wrapper.watch_events(REF)
        assert delegate.calls == []
        assert await wrapper.stat_artifact(StatArtifactQuery(ArtifactRef(REF, "x"))) == "stat"
        assert delegate.calls == ["stat"]

    asyncio.run(run())


def test_malformed_dtos_are_invalid_request_without_repr_or_authorization() -> None:
    class Secret:
        def __repr__(self) -> str:
            raise AssertionError("repr must not execute")

    async def run() -> None:
        delegate = SpyDelegate()
        wrapper = bind_access(delegate, PRINCIPAL, _authorizer(SESSION_READ))
        for method, value in (
            ("get_session", Secret()),
            ("submit_turn", Secret()),
            ("stat_artifact", Secret()),
            ("pending_approval", Secret()),
            ("resume_turn", Secret()),
        ):
            with pytest.raises(InvalidRequestError) as exc:
                await getattr(wrapper, method)(value)
            assert "Secret" in str(exc.value)
            assert "repr must not execute" not in str(exc.value)
        assert delegate.calls == []

    asyncio.run(run())


def test_delegate_errors_propagate_as_the_same_exception_object() -> None:
    async def run() -> None:
        for error in (RuntimeError("ordinary"), KeyboardInterrupt()):
            delegate = SpyDelegate(error=error)
            wrapper = bind_access(delegate, PRINCIPAL, _authorizer(SESSION_READ))
            with pytest.raises(type(error)) as exc:
                await wrapper.get_session(GetSessionQuery(REF))
            assert exc.value is error

    asyncio.run(run())


def test_principal_is_bound_and_not_a_dto_field() -> None:
    assert {field.name for field in dataclasses.fields(SubmitTurnCommand)} != {"principal"}
    delegate = SpyDelegate()
    wrapper = bind_access(delegate, PRINCIPAL, _authorizer(SESSION_READ))
    assert not hasattr(wrapper, "delegate")
    assert not hasattr(wrapper, "authorizer")


def test_watch_authorizes_at_creation_and_returns_delegate_lease() -> None:
    delegate = SpyDelegate()
    wrapper = bind_access(delegate, PRINCIPAL, _authorizer(EVENTS_WATCH))
    lease = wrapper.watch_events(REF, event_filter=EventFilter())
    assert lease is delegate.watch_lease
    assert delegate.calls == ["watch"]

    denied = bind_access(delegate, PRINCIPAL, AclAuthorizer([]))
    with pytest.raises(PermissionDeniedError):
        denied.watch_events(REF)
    assert delegate.calls == ["watch"]


def test_real_local_service_can_be_wrapped_without_submit_permission() -> None:
    # This remains a narrow integration check: opening and reading are ACL-only
    # concerns here; the delegate is intentionally a small application spy.
    async def run() -> None:
        delegate = SpyDelegate()
        wrapper = bind_access(
            delegate,
            PRINCIPAL,
            _authorizer(SESSION_OPEN, SESSION_READ, ARTIFACTS_READ),
        )
        assert await wrapper.open_session(OpenSessionCommand(REF)) == "open"
        assert await wrapper.get_session(GetSessionQuery(REF)) == "get"
        with pytest.raises(PermissionDeniedError):
            await wrapper.submit_turn(SubmitTurnCommand(REF, "unchanged"))
        assert delegate.calls == ["open", "get"]

    asyncio.run(run())


def test_watch_session_malformed_ref_is_invalid_request_not_permission() -> None:
    async def run() -> None:
        wrapper = bind_access(SpyDelegate(), PRINCIPAL, AclAuthorizer([]))
        with pytest.raises(InvalidRequestError):
            wrapper.watch_events(None)  # type: ignore[arg-type]
        with pytest.raises(InvalidRequestError):
            await wrapper.get_session(GetSessionQuery(None))  # type: ignore[arg-type]

    asyncio.run(run())
