"""Project-level reasoning default: protocol, ACL, persistence and rollback.

Covers `runtime.project.thinking.set` (the project-scoped default the console
settings dialog consumes), the dedicated `project.thinking` capability that no
session-scoped grant can satisfy, the settings-layer persistence that survives a
daemon restart, and the rollback that keeps a failed write from silently changing
what newly opened sessions inherit.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from synapse.runtime.daemon.application import (
    apply_project_layer_thinking,
    apply_project_thinking_default,
)
from synapse.runtime.service import (
    PROJECT_THINKING,
    SESSION_READ,
    SESSION_REBIND,
    SESSION_THINKING,
    AclAuthorizer,
    AclGrant,
    GetRuntimeConfigQuery,
    InvalidRequestError,
    OpenSessionCommand,
    Principal,
    RuntimeConfigView,
    SetProjectThinkingLevelCommand,
    SetProjectThinkingLevelResult,
    SetThinkingLevelCommand,
    bind_access,
)
from synapse.runtime.service.access import _REQUIRED_DELEGATE_METHODS
from synapse.runtime.service.errors import PermissionDeniedError
from synapse.runtime.service.local import LocalAgentRuntimeService
from synapse.runtime.sessions import RuntimeManager
from synapse.runtime.sessions.ref import SessionRef
from synapse.runtime.transport import CAPABILITIES
from synapse.runtime.transport.client import RuntimeWebSocketClient
from synapse.runtime.transport.protocol import (
    METHODS,
    ProtocolError,
    decode_params,
    dispatch,
)
from synapse.settings.config_paths import (
    read_project_thinking_default,
    set_project_reasoning_effort,
)

METHOD = "runtime.project.thinking.set"
PROJECT = "project-a"


class FakeRegistry:
    """Minimal model registry exposing one whitelist for every model."""

    default = "m"

    def __init__(self, levels: tuple[str, ...]) -> None:
        self.levels = levels

    def allowed_thinking_levels(self, _model: object) -> tuple[str, ...]:
        return self.levels

    def list_names(self) -> tuple[str, ...]:
        return ("m",)


def _settings(**overrides: object) -> Any:
    base: dict[str, object] = {
        "active_model": None,
        "model": "m",
        "enable_thinking": True,
        "reasoning_effort": "high",
        "workspace": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _patch_registry(monkeypatch: pytest.MonkeyPatch, levels: tuple[str, ...]) -> None:
    import synapse.runtime.service.config_source as config_source

    monkeypatch.setattr(config_source, "registry_from_settings", lambda _s: FakeRegistry(levels))
    monkeypatch.setattr(config_source, "load_mcp_server_configs", lambda **kwargs: [])


# ---------------------------------------------------------------------------
# Protocol: registration, strict decode, dispatch
# ---------------------------------------------------------------------------


def test_project_thinking_method_is_registered_and_decodes_strictly() -> None:
    assert METHOD in METHODS

    decoded = decode_params(METHOD, {"project_id": PROJECT, "level": "high"})
    assert isinstance(decoded, SetProjectThinkingLevelCommand)
    assert decoded.project_id == PROJECT
    assert decoded.level == "high"
    assert decoded.command_id

    explicit = decode_params(
        METHOD, {"project_id": PROJECT, "level": "low", "command_id": "cmd-1"}
    )
    assert isinstance(explicit, SetProjectThinkingLevelCommand)
    assert explicit.command_id == "cmd-1"

    for params in (
        {"project_id": PROJECT},
        {"level": "high"},
        {"project_id": PROJECT, "level": "high", "extra": 1},
        {"project_id": "", "level": "high"},
        {"project_id": PROJECT, "level": ""},
        {"project_id": PROJECT, "level": 3},
        {"project_id": 3, "level": "high"},
        # A session ref is never accepted here: this port is project-scoped.
        {"session": {"project_id": PROJECT, "thread_id": "t"}, "level": "high"},
    ):
        with pytest.raises(ProtocolError) as caught:
            decode_params(METHOD, params)
        assert caught.value.service_code == "invalid_params"


class _FakeService:
    def __init__(self) -> None:
        self.seen: object = None

    async def set_project_thinking_level(self, command: object) -> object:
        self.seen = command
        return SetProjectThinkingLevelResult(
            command.command_id, command.project_id, "high"  # type: ignore[attr-defined]
        )


def test_project_thinking_dispatch_routes_to_the_service() -> None:
    service = _FakeService()
    result = asyncio.run(
        dispatch(service, METHOD, {"project_id": PROJECT, "level": "high"})
    )
    assert isinstance(service.seen, SetProjectThinkingLevelCommand)
    assert isinstance(result, SetProjectThinkingLevelResult)
    assert (result.project_id, result.level) == (PROJECT, "high")


# ---------------------------------------------------------------------------
# ACL: a dedicated project capability, never a session grant
# ---------------------------------------------------------------------------


def test_project_thinking_is_not_authorized_by_any_session_capability() -> None:
    for capability in (SESSION_READ, SESSION_THINKING, SESSION_REBIND):
        authorizer = AclAuthorizer([AclGrant("subject", PROJECT, frozenset({capability}))])
        with pytest.raises(PermissionDeniedError):
            authorizer.authorize_project(Principal("subject"), PROJECT_THINKING, PROJECT)
    allowed = AclAuthorizer(
        [AclGrant("subject", PROJECT, frozenset({PROJECT_THINKING}))]
    )
    allowed.authorize_project(Principal("subject"), PROJECT_THINKING, PROJECT)


def test_project_thinking_rejects_a_thread_scoped_grant() -> None:
    """A grant scoped to one thread must not change a project-wide default."""

    authorizer = AclAuthorizer(
        [
            AclGrant(
                "subject",
                PROJECT,
                frozenset({PROJECT_THINKING}),
                frozenset({"t"}),
            )
        ]
    )
    with pytest.raises(PermissionDeniedError):
        authorizer.authorize_project(Principal("subject"), PROJECT_THINKING, PROJECT)


class _StubDelegate:
    """Minimal delegate satisfying the wrapper's required method set."""

    def __init__(self) -> None:
        self.commands: list[object] = []

        async def _noop(*args: object, **kwargs: object) -> None:
            return None

        for name in _REQUIRED_DELEGATE_METHODS:
            setattr(self, name, _noop)

        async def set_project_thinking_level(
            command: SetProjectThinkingLevelCommand,
        ) -> object:
            self.commands.append(command)
            return SetProjectThinkingLevelResult(
                command.command_id, command.project_id, "high"
            )

        self.set_project_thinking_level = set_project_thinking_level  # type: ignore[method-assign]


def _wrapper(*capabilities: str) -> Any:
    delegate = _StubDelegate()
    grant = AclGrant("subject", PROJECT, frozenset(capabilities))
    return bind_access(delegate, Principal("subject"), AclAuthorizer([grant]))


def test_wrapper_requires_the_project_capability_and_delegates() -> None:
    command = SetProjectThinkingLevelCommand(PROJECT, "high")

    allowed = _wrapper(PROJECT_THINKING)
    result = asyncio.run(allowed.set_project_thinking_level(command))
    assert isinstance(result, SetProjectThinkingLevelResult)
    assert result.command_id == command.command_id
    assert allowed._delegate.commands == [command]

    for capability in (SESSION_READ, SESSION_THINKING, SESSION_REBIND):
        denied = _wrapper(capability)
        with pytest.raises(PermissionDeniedError):
            asyncio.run(denied.set_project_thinking_level(command))
        assert denied._delegate.commands == []


def test_wrapper_reports_the_feature_unavailable_on_an_old_delegate() -> None:
    delegate = _StubDelegate()
    del delegate.set_project_thinking_level
    grant = AclGrant("subject", PROJECT, frozenset({PROJECT_THINKING}))
    wrapper = bind_access(delegate, Principal("subject"), AclAuthorizer([grant]))
    with pytest.raises(InvalidRequestError) as caught:
        asyncio.run(
            wrapper.set_project_thinking_level(SetProjectThinkingLevelCommand(PROJECT, "high"))
        )
    assert caught.value.code == "invalid_request"


def test_wrapper_rejects_a_malformed_project_command() -> None:
    wrapper = _wrapper(PROJECT_THINKING)
    with pytest.raises(InvalidRequestError):
        asyncio.run(wrapper.set_project_thinking_level(object()))  # type: ignore[arg-type]
    with pytest.raises(InvalidRequestError):
        asyncio.run(
            wrapper.set_project_thinking_level(  # type: ignore[arg-type]
                SimpleNamespace(project_id="", level="high", command_id="c")
            )
        )


# ---------------------------------------------------------------------------
# Persistence: the project settings layer
# ---------------------------------------------------------------------------


def test_set_project_reasoning_effort_writes_the_project_layer(tmp_path: Path) -> None:
    path = set_project_reasoning_effort("high", workspace=tmp_path)
    assert path == tmp_path / ".synapse" / "settings.json"
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "enable_thinking": True,
        "reasoning_effort": "high",
    }

    # Existing keys survive, and "off" keeps the level so it can be restored.
    set_project_reasoning_effort("low", workspace=tmp_path)
    path.write_text(
        json.dumps({**json.loads(path.read_text(encoding="utf-8")), "theme": "dark"}),
        encoding="utf-8",
    )
    set_project_reasoning_effort("off", workspace=tmp_path)
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written == {"enable_thinking": False, "reasoning_effort": "low", "theme": "dark"}

    # The write is atomic: no temp file survives.
    assert [p.name for p in (tmp_path / ".synapse").iterdir()] == ["settings.json"]

    with pytest.raises(ValueError):
        set_project_reasoning_effort("", workspace=tmp_path)
    with pytest.raises(ValueError):
        set_project_reasoning_effort(3, workspace=tmp_path)  # type: ignore[arg-type]


def test_apply_project_thinking_default_validates_and_persists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_registry(monkeypatch, ("off", "low", "high"))
    settings = _settings(workspace=tmp_path)

    label = apply_project_thinking_default(settings, "low", workspace=tmp_path)
    assert label == "low"
    assert read_project_thinking_default(tmp_path) == "low"
    assert json.loads((tmp_path / ".synapse" / "settings.json").read_text(encoding="utf-8")) == {
        "enable_thinking": True,
        "reasoning_effort": "low",
    }
    # The caller's settings object is never mutated: the value takes effect through
    # the project layer, which is what newly opened sessions read.
    assert (settings.enable_thinking, settings.reasoning_effort) == (True, "high")

    # A level outside the live whitelist is refused before anything is written.
    with pytest.raises(ValueError):
        apply_project_thinking_default(settings, "max", workspace=tmp_path)
    assert read_project_thinking_default(tmp_path) == "low"


def test_apply_project_thinking_default_leaves_no_partial_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_registry(monkeypatch, ("off", "low", "high"))
    settings = _settings(workspace=tmp_path)
    apply_project_thinking_default(settings, "low", workspace=tmp_path)

    def _boom(*args: object, **kwargs: object) -> Path:
        raise OSError("disk is read-only")

    import synapse.settings.config_paths as config_paths

    monkeypatch.setattr(config_paths, "set_project_reasoning_effort", _boom)
    with pytest.raises(OSError):
        apply_project_thinking_default(settings, "high", workspace=tmp_path)
    # The atomic write means the previous default is intact, so a failed write can
    # never leave a half-applied project default behind.
    assert read_project_thinking_default(tmp_path) == "low"
    assert [p.name for p in (tmp_path / ".synapse").iterdir()] == ["settings.json"]


def test_apply_project_layer_thinking_seeds_a_new_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_registry(monkeypatch, ("off", "low", "high"))
    set_project_reasoning_effort("low", workspace=tmp_path)

    settings = _settings(reasoning_effort="max")  # the model profile's seed
    apply_project_layer_thinking(settings, tmp_path)
    assert settings.reasoning_effort == "low"

    # "off" disables thinking instead of setting an effort.
    set_project_reasoning_effort("off", workspace=tmp_path)
    off = _settings(reasoning_effort="max")
    apply_project_layer_thinking(off, tmp_path)
    assert off.enable_thinking is False

    # A stale default outside the current whitelist must not block a session.
    set_project_reasoning_effort("high", workspace=tmp_path)
    _patch_registry(monkeypatch, ("off", "low"))
    stale = _settings(reasoning_effort="low")
    apply_project_layer_thinking(stale, tmp_path)
    assert stale.reasoning_effort == "low"


def test_read_project_thinking_default_reports_nothing_safely(tmp_path: Path) -> None:
    assert read_project_thinking_default(tmp_path) is None
    set_project_reasoning_effort("off", workspace=tmp_path)
    assert read_project_thinking_default(tmp_path) == "off"
    (tmp_path / ".synapse" / "settings.json").write_text("{ not json", encoding="utf-8")
    assert read_project_thinking_default(tmp_path) is None


# ---------------------------------------------------------------------------
# Service port: validation, persistence and the refreshed config projection
# ---------------------------------------------------------------------------


def _manager(settings: Any, tmp_path: Path) -> RuntimeManager:
    return RuntimeManager(
        settings=settings,
        agent_factory=lambda thread_id, shared: SimpleNamespace(),
        project_id=PROJECT,
        project_thinking_writer=lambda level, current: apply_project_thinking_default(
            current, level, workspace=tmp_path
        ),
    )


def test_service_persists_the_project_default_and_projects_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_registry(monkeypatch, ("off", "low", "high"))
    settings = _settings(workspace=tmp_path)
    manager = _manager(settings, tmp_path)
    service = LocalAgentRuntimeService(lambda project_id: manager)

    result = asyncio.run(
        service.set_project_thinking_level(SetProjectThinkingLevelCommand(PROJECT, "low"))
    )
    assert (result.project_id, result.level) == (PROJECT, "low")
    assert json.loads((tmp_path / ".synapse" / "settings.json").read_text(encoding="utf-8"))[
        "reasoning_effort"
    ] == "low"

    view = asyncio.run(
        service.get_runtime_config(GetRuntimeConfigQuery(SessionRef(PROJECT, "t")))
    )
    assert isinstance(view, RuntimeConfigView)
    assert view.project_thinking_level == "low"
    assert view.can_set_project_thinking is True

    # A level outside the whitelist is rejected and nothing is rewritten.
    with pytest.raises(InvalidRequestError):
        asyncio.run(
            service.set_project_thinking_level(
                SetProjectThinkingLevelCommand(PROJECT, "max")
            )
        )
    with pytest.raises(InvalidRequestError):
        asyncio.run(
            service.set_project_thinking_level(
                SetProjectThinkingLevelCommand(PROJECT, "x" * 200)
            )
        )
    # An empty project id can never reach the service: the DTO refuses it first.
    with pytest.raises(ValueError):
        SetProjectThinkingLevelCommand("", "low")
    assert json.loads((tmp_path / ".synapse" / "settings.json").read_text(encoding="utf-8"))[
        "reasoning_effort"
    ] == "low"


def test_service_reports_no_project_capability_without_a_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_registry(monkeypatch, ("low", "high"))
    settings = _settings(workspace=tmp_path)
    manager = RuntimeManager(
        settings=settings,
        agent_factory=lambda thread_id, shared: SimpleNamespace(),
        project_id=PROJECT,
    )
    service = LocalAgentRuntimeService(lambda project_id: manager)

    view = asyncio.run(
        service.get_runtime_config(GetRuntimeConfigQuery(SessionRef(PROJECT, "t")))
    )
    assert view.can_set_project_thinking is False
    # The project default is still reported: it comes from the project settings.
    assert view.project_thinking_level == "high"


def test_session_thinking_write_keeps_advertising_the_project_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session-level write must not degrade the project row to unknown/read-only.

    The console maps ``SetThinkingLevelResult.view`` straight into its state, so
    that refreshed view has to carry the same project-scoped fields as
    ``runtime.config.get``.  Regression: the write built the view without the
    project settings, so any reasoning-level change flipped the settings dialog
    to "unknown + read-only" even though the write port exists.
    """
    _patch_registry(monkeypatch, ("off", "low", "high"))
    settings = _settings(workspace=tmp_path)
    manager = RuntimeManager(
        settings=settings,
        agent_factory=lambda thread_id, shared: SimpleNamespace(),
        thinking_rebind_factory=lambda thread_id, level, binding, shared: (
            SimpleNamespace(),
            SimpleNamespace(**{**vars(settings), "reasoning_effort": level}),
        ),
        project_id=PROJECT,
        project_thinking_writer=lambda level, current: apply_project_thinking_default(
            current, level, workspace=tmp_path
        ),
    )
    service = LocalAgentRuntimeService(lambda project_id: manager)
    ref = SessionRef(PROJECT, "t")
    asyncio.run(service.open_session(OpenSessionCommand(ref)))

    result = asyncio.run(service.set_thinking_level(SetThinkingLevelCommand(ref, "low")))

    assert result.view.project_thinking_level == "high"
    assert result.view.can_set_project_thinking is True


# ---------------------------------------------------------------------------
# Client: strict result decoding
# ---------------------------------------------------------------------------


class _FakeTransport:
    def __init__(self, result: object) -> None:
        self.inbox: asyncio.Queue[str] = asyncio.Queue()
        self.frames: list[dict[str, object]] = []
        self.result = result

    async def send(self, message: str) -> None:
        frame = json.loads(message)
        self.frames.append(frame)
        if frame["method"] == "runtime.protocol.negotiate":
            response: object = {
                "wire_version": "1",
                "supported_versions": ["1"],
                "capabilities": CAPABILITIES,
            }
        else:
            response = self.result
        await self.inbox.put(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": frame["id"],
                    "meta": {"wire_version": "1"},
                    "result": response,
                }
            )
        )

    async def recv(self) -> str:
        return await self.inbox.get()

    async def close(self) -> None:
        return None


def _client(result: object) -> tuple[RuntimeWebSocketClient, _FakeTransport]:
    fake = _FakeTransport(result)
    client = RuntimeWebSocketClient("ws://loopback", connect_factory=lambda *a, **k: fake)
    return client, fake


def test_client_roundtrips_the_project_thinking_write() -> None:
    command = SetProjectThinkingLevelCommand(PROJECT, "high", "cmd-1")
    client, fake = _client(
        {"command_id": "cmd-1", "project_id": PROJECT, "level": "high"}
    )
    result = asyncio.run(client.set_project_thinking_level(command))
    assert result == SetProjectThinkingLevelResult("cmd-1", PROJECT, "high")
    frame = next(f for f in fake.frames if f["method"] == METHOD)
    assert frame["params"] == {
        "project_id": PROJECT,
        "level": "high",
        "command_id": "cmd-1",
    }


def test_client_ignores_additive_project_thinking_fields() -> None:
    async def body() -> None:
        command = SetProjectThinkingLevelCommand(PROJECT, "high", "cmd-1")
        client, _ = _client(
            {"command_id": "cmd-1", "project_id": PROJECT, "level": "high", "path": "/x"}
        )
        try:
            assert await client.set_project_thinking_level(command) == (
                SetProjectThinkingLevelResult("cmd-1", PROJECT, "high")
            )
        finally:
            await client.close()
    asyncio.run(body())


def test_client_rejects_malformed_project_thinking_results() -> None:
    from synapse.runtime.transport.client import ProtocolTransportError

    command = SetProjectThinkingLevelCommand(PROJECT, "high", "cmd-1")
    for result in (
        {"command_id": "cmd-1", "project_id": PROJECT},
        {"command_id": "other", "project_id": PROJECT, "level": "high"},
        {"command_id": "cmd-1", "project_id": "other", "level": "high"},
        {"command_id": "cmd-1", "project_id": PROJECT, "level": ""},
        {"command_id": "cmd-1", "project_id": PROJECT, "level": 3},
        ["not", "a", "dict"],
    ):
        client, _ = _client(result)
        with pytest.raises(ProtocolTransportError):
            asyncio.run(client.set_project_thinking_level(command))
