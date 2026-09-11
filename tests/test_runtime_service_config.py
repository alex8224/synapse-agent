"""Read-only runtime configuration service slice tests.

Covers the pure DTO bounds, the whitelisted settings projection (secret
sentinels must never reach the view), LocalAgentRuntimeService settings
resolution (session-bound vs project), ACL deny-before-read, and old-delegate
compatibility on the access wrapper.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from types import SimpleNamespace

import pytest

from synapse.runtime.service import (
    MAX_RUNTIME_CONFIG_MCP_SERVERS,
    MAX_RUNTIME_CONFIG_MODELS,
    MAX_RUNTIME_CONFIG_TEXT_BYTES,
    MAX_RUNTIME_CONFIG_THINKING_LEVELS,
    AccessControlledAgentRuntimeService,
    AclAuthorizer,
    AclGrant,
    ClosedError,
    ConfigOverflowError,
    GetRuntimeConfigQuery,
    InvalidRequestError,
    LocalAgentRuntimeService,
    McpServerView,
    NotFoundError,
    PermissionDeniedError,
    Principal,
    RouterClosedError,
    RuntimeConfigView,
    bind_access,
)
from synapse.runtime.sessions import RuntimeManager
from synapse.runtime.sessions.ref import SessionRef
from synapse.runtime.transport.protocol import project_result

REF = SessionRef(project_id="p1", thread_id="thread-a")
OTHER_THREAD = SessionRef(project_id="p1", thread_id="thread-b")
PRINCIPAL = Principal("subject-a")

SECRET = "SENTINEL-SECRET-KEY-VALUE"


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# DTO shape and bounds
# ---------------------------------------------------------------------------


def test_config_dtos_are_frozen_slotted() -> None:
    for dto in (
        GetRuntimeConfigQuery(REF),
        McpServerView("files", "stdio", True),
        RuntimeConfigView(
            current_model="m",
            available_models=("m",),
            thinking_level=None,
            thinking_levels=("high",),
            mcp_servers=(),
            mcp_enabled=True,
        ),
    ):
        assert dataclasses.is_dataclass(dto)
        assert type(dto).__dataclass_params__.frozen is True
        assert hasattr(type(dto), "__slots__")


def test_config_query_validates_session_ref() -> None:
    assert GetRuntimeConfigQuery(REF).session == REF
    for bad in (None, "p:t", object(), SessionRef("", "t"), SessionRef("p", "")):
        with pytest.raises(ValueError):
            GetRuntimeConfigQuery(bad)  # type: ignore[arg-type]


def test_mcp_server_view_bounds() -> None:
    view = McpServerView("files", "streamable_http", False, tool_prefix="mcp__files")
    assert view.tool_prefix == "mcp__files"
    assert McpServerView("files", "stdio", True).tool_prefix is None
    for kwargs in (
        {"name": "", "transport": "stdio", "enabled": True},
        {"name": "x\x00y", "transport": "stdio", "enabled": True},
        {"name": "x" * (MAX_RUNTIME_CONFIG_TEXT_BYTES + 1), "transport": "stdio", "enabled": True},
        {"name": "files", "transport": "", "enabled": True},
        {"name": "files", "transport": "stdio", "enabled": 1},
        {
            "name": "files",
            "transport": "stdio",
            "enabled": True,
            "tool_prefix": "x" * (MAX_RUNTIME_CONFIG_TEXT_BYTES + 1),
        },
    ):
        with pytest.raises(ValueError):
            McpServerView(**kwargs)  # type: ignore[arg-type]


def test_runtime_config_view_bounds() -> None:
    base = {
        "current_model": "m",
        "available_models": ("m",),
        "thinking_level": None,
        "thinking_levels": ("high",),
        "mcp_servers": (),
        "mcp_enabled": True,
    }
    view = RuntimeConfigView(**base)  # type: ignore[arg-type]
    assert view.can_set_thinking is False
    assert view.can_toggle_mcp_global is False

    too_many_models = ("m",) * (MAX_RUNTIME_CONFIG_MODELS + 1)
    with pytest.raises(ValueError):
        RuntimeConfigView(**{**base, "available_models": too_many_models})  # type: ignore[arg-type]
    too_many_levels = ("high",) * (MAX_RUNTIME_CONFIG_THINKING_LEVELS + 1)
    with pytest.raises(ValueError):
        RuntimeConfigView(**{**base, "thinking_levels": too_many_levels})  # type: ignore[arg-type]
    too_many_servers = (McpServerView("s", "stdio", True),) * (MAX_RUNTIME_CONFIG_MCP_SERVERS + 1)
    with pytest.raises(ValueError):
        RuntimeConfigView(**{**base, "mcp_servers": too_many_servers})  # type: ignore[arg-type]

    for field in ("current_model",):
        with pytest.raises(ValueError):
            RuntimeConfigView(**{**base, field: "x" * (MAX_RUNTIME_CONFIG_TEXT_BYTES + 1)})  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        RuntimeConfigView(**{**base, "mcp_enabled": 1})  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        RuntimeConfigView(**{**base, "thinking_level": ""})  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        RuntimeConfigView(**{**base, "available_models": ("m", 3)})  # type: ignore[arg-type]


def test_view_wire_projection_is_whitelisted_json() -> None:
    view = RuntimeConfigView(
        current_model="m1",
        available_models=("m1", "m2"),
        thinking_level="high",
        thinking_levels=("low", "high"),
        mcp_servers=(McpServerView("files", "stdio", True, tool_prefix="mcp__files"),),
        mcp_enabled=True,
    )
    projected = project_result(view)
    assert projected == {
        "current_model": "m1",
        "available_models": ["m1", "m2"],
        "thinking_level": "high",
        "thinking_levels": ["low", "high"],
        "mcp_servers": [
            {"name": "files", "transport": "stdio", "enabled": True, "tool_prefix": "mcp__files"}
        ],
        "mcp_enabled": True,
        "can_set_thinking": False,
        "can_toggle_mcp_global": False,
    }
    # A view can never carry arbitrary settings keys, so a secret sentinel that
    # exists only on the source settings object cannot leak into JSON.
    assert SECRET not in json.dumps(projected, allow_nan=False)


# ---------------------------------------------------------------------------
# config_source: whitelisted projection (no secret leakage, no truncation)
# ---------------------------------------------------------------------------

_SENTINEL_FIELDS = {
    "openai_api_key": SECRET,
    "anthropic_api_key": SECRET,
    "openai_base_url": "https://gateway.example.invalid/secret",
    "api_key_env": "VISION_API_KEY",
    "mcp_env": {"TOKEN": SECRET},
    "active_goal": "should-never-leak",
}


def _source_settings(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "active_model": None,
        "model": "openai:project-model",
        "enable_thinking": True,
        "reasoning_effort": "high",
        "enable_mcp": True,
        "mcp_config_path": None,
        "mcp_servers_json": None,
        "workspace": "/sandbox/workspace",
    }
    values.update(overrides)
    values.update(_SENTINEL_FIELDS)
    return SimpleNamespace(**values)


class FakeRegistry:
    def __init__(
        self,
        names: list[str],
        default: str,
        levels: list[str] | None = None,
        default_thinking: str | None = None,
    ) -> None:
        self.names = names
        self.default = default
        self.levels = levels or ["off", "minimal", "low", "medium", "high", "max"]
        self.default_thinking = default_thinking

    def list_names(self) -> list[str]:
        return list(self.names)

    def allowed_thinking_levels(self, name: str | None = None) -> list[str]:
        del name
        return list(self.levels)


class FakeMcpServer:
    def __init__(self, name: str, transport: str, enabled: bool, tool_prefix: str | None) -> None:
        self.name = name
        self.transport = transport
        self.enabled = enabled
        self.tool_prefix = tool_prefix
        # Never-projected secret-laden fields that the loader would normally fill.
        self.command = f"cmd-{name}"
        self.args = ["--secret", SECRET]
        self.env = {"TOKEN": SECRET}
        self.url = "https://mcp.example.invalid/secret"
        self.headers = {"Authorization": f"Bearer {SECRET}"}
        self.api_key = SECRET


def _view_of(
    settings: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    *,
    servers: list[object] | None = None,
) -> RuntimeConfigView:
    import synapse.runtime.service.config_source as config_source

    monkeypatch.setattr(
        config_source,
        "registry_from_settings",
        lambda _s: FakeRegistry(["openai:a", "openai:b"], "openai:b"),
    )
    monkeypatch.setattr(
        config_source,
        "load_mcp_server_configs",
        lambda **kwargs: list(servers or []),
    )
    return config_source.build_config_view(settings, session=REF)


def test_config_source_projects_whitelist_without_secret_fields(monkeypatch) -> None:
    settings = _source_settings(
        active_model=None,
        model="openai:project-model",
        reasoning_effort="high",
    )
    server = FakeMcpServer("files", "stdio", True, "mcp__files")
    view = _view_of(settings, monkeypatch, servers=[server])
    assert view.current_model == "openai:b"  # registry default wins over model fallback
    assert view.available_models == ("openai:a", "openai:b")
    assert view.thinking_level == "high"
    # The session-scoped reasoning-level write port now exists, so the view
    # advertises the capability as editable; the global MCP toggle stays False.
    assert view.can_set_thinking is True
    assert view.can_toggle_mcp_global is False
    assert len(view.mcp_servers) == 1
    mcp = view.mcp_servers[0]
    assert (mcp.name, mcp.transport, mcp.enabled, mcp.tool_prefix) == (
        "files",
        "stdio",
        True,
        "mcp__files",
    )

    raw = json.dumps(dataclasses.asdict(view), allow_nan=False)
    for key in ("command", "args", "env", "url", "headers", "api_key"):
        assert key not in raw
    assert SECRET not in raw
    assert "should-never-leak" not in raw
    assert set(dataclasses.asdict(view)) == {
        "current_model",
        "available_models",
        "thinking_level",
        "thinking_levels",
        "mcp_servers",
        "mcp_enabled",
        "can_set_thinking",
        "can_toggle_mcp_global",
    }


def test_config_source_current_model_prefers_active_alias(monkeypatch) -> None:
    settings = _source_settings(active_model="openai:a")
    view = _view_of(settings, monkeypatch)
    assert view.current_model == "openai:a"


def test_config_source_mcp_disabled_skips_loader(monkeypatch) -> None:
    import synapse.runtime.service.config_source as config_source

    settings = _source_settings(enable_mcp=False)
    loaded: list[bool] = []
    monkeypatch.setattr(
        config_source,
        "registry_from_settings",
        lambda _s: FakeRegistry(["m"], "m"),
    )
    monkeypatch.setattr(
        config_source,
        "load_mcp_server_configs",
        lambda **kwargs: loaded.append(True) or [],
    )
    view = config_source.build_config_view(settings, session=REF)
    assert view.mcp_enabled is False
    assert view.mcp_servers == ()
    assert loaded == []


def test_config_source_overflows_raise_typed_error(monkeypatch) -> None:
    import synapse.runtime.service.config_source as config_source

    monkeypatch.setattr(config_source, "registry_from_settings", lambda _s: None)
    with pytest.raises(ConfigOverflowError) as caught:
        config_source.build_config_view(_source_settings(), session=REF)
    assert caught.value.code == "config_overflow"

    monkeypatch.setattr(
        config_source,
        "registry_from_settings",
        lambda _s: FakeRegistry(["m"] * (MAX_RUNTIME_CONFIG_MODELS + 1), "m"),
    )
    with pytest.raises(ConfigOverflowError):
        config_source.build_config_view(_source_settings(), session=REF)

    monkeypatch.setattr(
        config_source,
        "registry_from_settings",
        lambda _s: FakeRegistry(
            ["m"], "m", levels=["high"] * (MAX_RUNTIME_CONFIG_THINKING_LEVELS + 1)
        ),
    )
    with pytest.raises(ConfigOverflowError):
        config_source.build_config_view(_source_settings(), session=REF)

    monkeypatch.setattr(
        config_source,
        "registry_from_settings",
        lambda _s: FakeRegistry(["m"], "m"),
    )
    too_many = [
        FakeMcpServer(f"s{i}", "stdio", True, None)
        for i in range(MAX_RUNTIME_CONFIG_MCP_SERVERS + 1)
    ]
    monkeypatch.setattr(config_source, "load_mcp_server_configs", lambda **kwargs: too_many)
    with pytest.raises(ConfigOverflowError):
        config_source.build_config_view(_source_settings(), session=REF)


def test_config_source_does_not_swallow_core_read_errors(monkeypatch) -> None:
    """Registry / MCP loader failures propagate; only view overflow maps."""
    import synapse.runtime.service.config_source as config_source

    monkeypatch.setattr(
        config_source,
        "registry_from_settings",
        lambda _s: FakeRegistry(["m"], "m"),
    )
    monkeypatch.setattr(
        config_source,
        "load_mcp_server_configs",
        lambda **kwargs: (_ for _ in ()).throw(OSError("disk read failed")),
    )
    with pytest.raises(OSError, match="disk read failed"):
        config_source.build_config_view(_source_settings(), session=REF)


# ---------------------------------------------------------------------------
# LocalAgentRuntimeService resolution: session-bound vs project settings
# ---------------------------------------------------------------------------

def _manager(project_settings: object) -> RuntimeManager:
    manager = RuntimeManager(
        settings=project_settings,
        agent_factory=lambda thread_id, shared: object(),
        project_id=REF.project_id,
    )
    return manager


def _local(manager: RuntimeManager) -> LocalAgentRuntimeService:
    return LocalAgentRuntimeService(lambda project_id: manager)


def _project_settings() -> SimpleNamespace:
    return _source_settings(model="openai:project-model", reasoning_effort="medium")


def _session_settings() -> SimpleNamespace:
    return _source_settings(
        active_model="openai:session-bound-model",
        model="openai:project-model",
        reasoning_effort="high",
    )


def test_local_uses_project_settings_for_unopened_session(monkeypatch) -> None:
    import synapse.runtime.service.config_source as config_source

    project_settings = _project_settings()
    captured: list[object] = []
    monkeypatch.setattr(
        config_source,
        "registry_from_settings",
        lambda _s: FakeRegistry(["openai:project-model"], "openai:project-model"),
    )
    monkeypatch.setattr(config_source, "load_mcp_server_configs", lambda **kwargs: [])

    def spy_build(settings: object, *, session: SessionRef) -> RuntimeConfigView:
        captured.append(settings)
        del session
        return _dummy_view()

    monkeypatch.setattr(config_source, "build_config_view", spy_build)
    manager = _manager(project_settings)
    service = _local(manager)
    run(service.get_runtime_config(GetRuntimeConfigQuery(REF)))
    assert captured == [project_settings]  # project settings object identity
    # No session was opened by the query itself.
    assert manager.get_session_ref(REF) is None


def test_local_prefers_session_bound_settings_for_opened_session(monkeypatch) -> None:
    import synapse.runtime.service.config_source as config_source

    project_settings = _project_settings()
    monkeypatch.setattr(
        config_source,
        "registry_from_settings",
        lambda _s: FakeRegistry(
            ["openai:session-bound-model", "openai:project-model"],
            "openai:project-model",
        ),
    )
    monkeypatch.setattr(config_source, "load_mcp_server_configs", lambda **kwargs: [])
    captured: list[object] = []
    monkeypatch.setattr(
        config_source,
        "build_config_view",
        lambda settings, *, session: captured.append(settings) or _dummy_view(),
    )

    manager = _manager(project_settings)
    bound = _session_settings()
    manager._sessions[REF.thread_id] = SimpleNamespace(settings=bound)
    service = _local(manager)
    run(service.get_runtime_config(GetRuntimeConfigQuery(REF)))
    assert captured == [bound]  # session-bound settings win for an opened session

    # An unopened thread still falls back to the project settings object.
    run(service.get_runtime_config(GetRuntimeConfigQuery(OTHER_THREAD)))
    assert captured[-1] is project_settings


def _dummy_view() -> RuntimeConfigView:
    return RuntimeConfigView(
        current_model="session-bound",
        available_models=("session-bound",),
        thinking_level=None,
        thinking_levels=("high",),
        mcp_servers=(),
        mcp_enabled=True,
    )


def test_local_never_opens_or_builds_agent(monkeypatch) -> None:
    import synapse.runtime.service.config_source as config_source

    monkeypatch.setattr(
        config_source,
        "registry_from_settings",
        lambda _s: FakeRegistry(["m"], "m"),
    )
    monkeypatch.setattr(config_source, "load_mcp_server_configs", lambda **kwargs: [])
    manager = _manager(_project_settings())
    service = _local(manager)
    run(service.get_runtime_config(GetRuntimeConfigQuery(REF)))
    assert manager._sessions == {}


def test_local_config_unknown_project_maps_to_not_found() -> None:
    service = LocalAgentRuntimeService(lambda project_id: None)
    with pytest.raises(NotFoundError):
        run(service.get_runtime_config(GetRuntimeConfigQuery(REF)))


def test_local_config_closed_router_maps_to_closed() -> None:
    def provider(project_id: str):
        del project_id
        raise RouterClosedError("runtime is closed")

    service = LocalAgentRuntimeService(provider)
    with pytest.raises(ClosedError):
        run(service.get_runtime_config(GetRuntimeConfigQuery(REF)))


# ---------------------------------------------------------------------------
# Access wrapper: ACL deny-before-read and old-delegate compatibility
# ---------------------------------------------------------------------------


class _ConfigDelegate:
    """Minimal service that implements the required port methods plus config."""

    def __init__(self, *, support_config: bool = True) -> None:
        self.config_calls = 0
        self.support_config = support_config

    async def submit_turn(self, command):  # pragma: no cover - stub
        raise AssertionError("unused")

    async def open_session(self, command):  # pragma: no cover - stub
        raise AssertionError("unused")

    async def cancel_turn(self, command):  # pragma: no cover - stub
        raise AssertionError("unused")

    async def steer_turn(self, command):  # pragma: no cover - stub
        raise AssertionError("unused")

    async def resume_turn(self, command):  # pragma: no cover - stub
        raise AssertionError("unused")

    async def pending_approval(self, query):  # pragma: no cover - stub
        raise AssertionError("unused")

    async def close_session(self, command):  # pragma: no cover - stub
        raise AssertionError("unused")

    async def get_session(self, query):  # pragma: no cover - stub
        raise AssertionError("unused")

    async def stat_artifact(self, query):  # pragma: no cover - stub
        raise AssertionError("unused")

    async def list_artifacts(self, query):  # pragma: no cover - stub
        raise AssertionError("unused")

    async def read_artifact(self, query):  # pragma: no cover - stub
        raise AssertionError("unused")

    async def read_events(self, query):  # pragma: no cover - stub
        raise AssertionError("unused")

    def watch_events(self, session, **kwargs):  # pragma: no cover - stub
        raise AssertionError("unused")

    async def get_runtime_config(self, query: GetRuntimeConfigQuery) -> RuntimeConfigView:
        self.config_calls += 1
        return _dummy_view()


def _config_authorizer(*capabilities: str) -> AclAuthorizer:
    return AclAuthorizer(
        [AclGrant(PRINCIPAL.subject, REF.project_id, frozenset(capabilities), None)]
    )


def test_acl_rejects_config_read_without_touching_delegate() -> None:
    delegate = _ConfigDelegate()
    wrapper = bind_access(delegate, PRINCIPAL, AclAuthorizer([]))
    with pytest.raises(PermissionDeniedError):
        run(wrapper.get_runtime_config(GetRuntimeConfigQuery(REF)))
    assert delegate.config_calls == 0


def test_acl_allows_config_read_with_session_read_grant() -> None:
    delegate = _ConfigDelegate()
    wrapper = bind_access(delegate, PRINCIPAL, _config_authorizer("session.read"))
    view = run(wrapper.get_runtime_config(GetRuntimeConfigQuery(REF)))
    assert delegate.config_calls == 1
    assert view.current_model == "session-bound"


def test_access_wrapper_is_optional_and_old_delegates_compat() -> None:
    """Delegates without get_runtime_config still construct; the call reports unavailable."""
    old_methods = {
        name: value
        for name, value in _ConfigDelegate.__dict__.items()
        if name != "get_runtime_config"
    }
    OldDelegate = type("OldDelegate", (object,), old_methods)
    delegate = OldDelegate()
    wrapper = bind_access(delegate, PRINCIPAL, _config_authorizer("session.read"))
    # Construction succeeded despite the missing optional method.
    assert isinstance(wrapper, AccessControlledAgentRuntimeService)
    with pytest.raises(InvalidRequestError):
        run(wrapper.get_runtime_config(GetRuntimeConfigQuery(REF)))
