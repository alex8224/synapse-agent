"""Tests for multi-model registry, sessions, MCP config, and agent wiring."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import synapse.models.registry as registry_module
from synapse.config import load_settings
from synapse.integrations.mcp_client import load_mcp_server_configs
from synapse.models.profile import ModelProfile
from synapse.models_registry import (
    apply_thinking_to_settings,
    build_model_from_settings,
    format_model_status,
    is_thinking_token,
    registry_from_settings,
    settings_thinking_label,
)
from synapse.runtime.fs_permissions import build_filesystem_permissions
from synapse.runtime.harness import apply_harness_exclusions
from synapse.runtime.subagents import build_default_subagents
from synapse.sessions import (
    ModelBinding,
    SessionStore,
    apply_binding_to_settings,
    binding_from_settings,
    format_session_table,
    resolve_startup_binding,
)


def test_build_chat_model_resolves_lazy_factory_inside_registry(monkeypatch):
    """The internal factory call must not bypass module ``__getattr__``."""
    fake_model = MagicMock(name="chat-model")
    fake_factory = MagicMock(name="init-chat-model", return_value=fake_model)
    monkeypatch.setattr(registry_module, "_init_chat_model", fake_factory)
    monkeypatch.delitem(registry_module.__dict__, "init_chat_model", raising=False)
    registry = registry_module.ModelRegistry(
        profiles={"test": ModelProfile(name="test", model="test:demo")},
        default="test",
    )

    assert registry.build_chat_model() is fake_model
    fake_factory.assert_called_once_with("test:demo")


def test_registry_legacy_single_model(tmp_path: Path, monkeypatch):
    # Isolate from ~/.synapse/models.json layered discovery.
    monkeypatch.setattr(
        "synapse.settings.config_paths.user_config_dir",
        lambda: tmp_path / "nouser" / ".synapse",
    )
    monkeypatch.setattr("synapse.settings.config_paths.executable_config_dirs", lambda: [])
    settings = load_settings(
        workspace=tmp_path,
        model="openai:demo",
        checkpoint_backend="memory",
        models_config_path=None,
    )
    reg = registry_from_settings(settings)
    assert reg.default == "openai:demo"
    assert reg.get().model == "openai:demo"


def test_registry_from_models_config(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("MODEL", raising=False)
    monkeypatch.delenv("AGENT_ACTIVE_MODEL", raising=False)
    monkeypatch.delenv("AGENT_MODELS_CONFIG", raising=False)
    cfg = {
        "default": "fast",
        "models": {
            "fast": {"model": "openai:fast-model", "base_url": "http://127.0.0.1:9/v1"},
            "slow": {"model": "anthropic:claude-x"},
        },
    }
    path = tmp_path / "models.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    settings = load_settings(
        workspace=tmp_path,
        models_config_path=path,
        checkpoint_backend="memory",
        model="openai:ignored-by-config-default",
    )
    settings = settings.model_copy(update={"active_model": None, "model": "openai:ignored"})
    reg = registry_from_settings(settings)
    assert reg.default == "fast"
    assert reg.get("slow").model == "anthropic:claude-x"
    assert reg.list_names() == ["fast", "slow"]


def test_models_config_thinking_and_params(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("AGENT_MODELS_CONFIG", raising=False)
    monkeypatch.delenv("MODEL", raising=False)
    # Force the langchain_openai fallback path (asserts below inspect kwargs
    # passed to init_chat_model). The native Rust transport is covered by
    # test_rust_transport_used_when_native_available.
    monkeypatch.setenv("SYNAPSE_DISABLE_RUST_OPENAI", "1")
    cfg = {
        "default": "main",
        "models": {
            "main": {
                "model": "openai:demo",
                "thinking_level": "max",
                "temperature": 0.1,
                "max_tokens": 1234,
                "model_kwargs": {"foo": 1},
                "extra_body": {"bar": 2},
            },
            "quiet": {
                "model": "openai:demo",
                "thinking": "off",
            },
            "low": {
                "model": "openai:demo",
                "thinking": "low",
            },
        },
    }
    path = tmp_path / ".synapse" / "models.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg), encoding="utf-8")
    settings = load_settings(
        workspace=tmp_path,
        models_config_path=None,
        checkpoint_backend="memory",
    )
    assert settings.active_model == "main"
    assert settings.model == "openai:demo"
    assert settings.enable_thinking is True
    assert settings.reasoning_effort == "max"
    reg = registry_from_settings(settings)
    assert reg.get("main").extra["temperature"] == 0.1
    assert reg.get("main").extra["max_tokens"] == 1234
    assert reg.get("main").model_kwargs == {"foo": 1}
    assert reg.get("main").extra_body == {"bar": 2}
    assert reg.get("quiet").enable_thinking is False
    assert reg.get("low").reasoning_effort == "low"

    with patch("synapse.models.registry.init_chat_model") as init_mock:
        init_mock.return_value = MagicMock(name="chat")
        reg.build_chat_model("main", fallback_api_key="k")
        kwargs = init_mock.call_args.kwargs
        assert kwargs["temperature"] == 0.1
        assert kwargs["max_tokens"] == 1234
        assert kwargs["reasoning_effort"] == "max"
        assert kwargs["model_kwargs"]["foo"] == 1
        assert kwargs["extra_body"]["bar"] == 2
        assert kwargs["extra_body"]["thinking"]["type"] == "enabled"

        init_mock.reset_mock()
        reg.build_chat_model("quiet", fallback_api_key="k")
        kwargs = init_mock.call_args.kwargs
        assert kwargs["extra_body"]["thinking"]["type"] == "disabled"


def test_models_config_can_enable_responses_websocket(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("AGENT_MODELS_CONFIG", raising=False)
    monkeypatch.delenv("MODEL", raising=False)
    monkeypatch.delenv("OPENAI_WEBSOCKET", raising=False)
    # The http profile asserts the langchain_openai fallback path; keep the
    # native Rust transport out of this test.
    monkeypatch.setenv("SYNAPSE_DISABLE_RUST_OPENAI", "1")
    cfg = {
        "default": "ws",
        "models": {
            "ws": {
                "model": "openai:gpt-test",
                "api_key": "test-key",
                "websocket": True,
                "thinking": "off",
            },
            "http": {
                "model": "openai:gpt-http",
                "api_key": "http-key",
                "thinking": "off",
            },
        },
    }
    path = tmp_path / "models.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    settings = load_settings(
        workspace=tmp_path,
        models_config_path=path,
        checkpoint_backend="memory",
    )
    # A profile-local transport must not overwrite the global fallback. Otherwise
    # later profiles without an explicit value inherit WebSocket from this model.
    assert settings.openai_websocket is False
    reg = registry_from_settings(settings)
    assert reg.get("ws").websocket is True
    assert reg.get("http").websocket is None

    fake_model = MagicMock(name="websocket-chat-model")
    fake_client = MagicMock(name="async-http-client")
    fake_runtime = MagicMock(name="async-runtime")
    with (
        patch("synapse.models.registry.init_chat_model") as init_mock,
        patch(
            "synapse.integrations.llm_openai_websocket.ResponsesWebSocketChatOpenAI",
            return_value=fake_model,
        ) as ws_model,
        patch(
            "synapse.integrations.http_clients.build_openai_async_http_client",
            return_value=fake_client,
        ),
        patch("synapse.runtime.async_runtime.get_async_runtime", return_value=fake_runtime),
    ):
        built = reg.build_chat_model("ws")
        init_mock.return_value = MagicMock(name="http-chat-model")
        _, http_model = build_model_from_settings(settings, model_name="http")

    assert built is fake_model
    assert http_model is init_mock.return_value
    init_mock.assert_called_once()
    assert init_mock.call_args.args == ("openai:gpt-http",)
    assert init_mock.call_args.kwargs["use_responses_api"] is False
    assert ws_model.call_args.kwargs["model"] == "gpt-test"
    assert ws_model.call_args.kwargs["use_responses_api"] is True
    assert ws_model.call_args.kwargs["streaming"] is True
    assert fake_model._coding_websocket is True
    fake_runtime.track_connection.assert_called_once_with(fake_model)


def test_thinking_levels_array_and_session_override(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("AGENT_MODELS_CONFIG", raising=False)
    monkeypatch.delenv("MODEL", raising=False)
    # Assertions below inspect kwargs passed to init_chat_model (the
    # langchain_openai fallback); disable the native Rust transport here.
    monkeypatch.setenv("SYNAPSE_DISABLE_RUST_OPENAI", "1")
    cfg = {
        "default": "main",
        "thinking_levels": ["off", "low", "high", "max"],
        "default_thinking": "high",
        "models": {
            "main": {
                "model": "openai:demo",
                # no per-profile thinking: uses default_thinking
            },
            "restricted": {
                "model": "openai:demo",
                "thinking_levels": ["off", "low"],
                "thinking": "low",
            },
        },
    }
    path = tmp_path / ".synapse" / "models.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg), encoding="utf-8")
    settings = load_settings(
        workspace=tmp_path,
        models_config_path=None,
        checkpoint_backend="memory",
    )
    assert settings.active_model == "main"
    assert settings.enable_thinking is True
    assert settings.reasoning_effort == "high"

    reg = registry_from_settings(settings)
    assert reg.thinking_levels == ["off", "low", "high", "max"]
    assert reg.allowed_thinking_levels("main") == ["off", "low", "high", "max"]
    assert reg.allowed_thinking_levels("restricted") == ["off", "low"]

    # Session override must win over profile default when building via settings.
    settings.enable_thinking = True
    settings.reasoning_effort = "low"
    with patch("synapse.models.registry.init_chat_model") as init_mock:
        init_mock.return_value = MagicMock(name="chat")
        build_model_from_settings(settings, model_name="main")
        kwargs = init_mock.call_args.kwargs
        assert kwargs["reasoning_effort"] == "low"
        assert kwargs["extra_body"]["thinking"]["type"] == "enabled"
        assert callable(kwargs["api_key"])
        assert kwargs["http_async_client"] is not None
        assert "http_client" not in kwargs

    # Disallowed level for restricted profile.
    try:
        apply_thinking_to_settings(
            settings,
            "max",
            allowed=reg.allowed_thinking_levels("restricted"),
        )
        raised = False
    except ValueError:
        raised = True
    assert raised is True

    apply_thinking_to_settings(
        settings, "off", allowed=reg.allowed_thinking_levels("restricted")
    )
    assert settings.enable_thinking is False


def test_profile_api_key_not_overridden_by_openai_fallback(tmp_path: Path, monkeypatch):
    """Anthropic/plaintext profile key must win over residual OPENAI_API_KEY."""
    monkeypatch.delenv("AGENT_MODELS_CONFIG", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-stale")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = {
        "default": "grok",
        "models": {
            "deep": {
                "model": "openai:deepseek-v4-pro",
                "api_key": "sk-openai-profile",
                "base_url": "http://openai.example/v1",
            },
            "grok": {
                "model": "anthropic:grok-4.5",
                "api_key": "sk-local-test-key",
                "base_url": "http://localhost:8317",
            },
        },
    }
    path = tmp_path / ".synapse" / "models.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg), encoding="utf-8")

    settings = load_settings(
        workspace=tmp_path,
        models_config_path=None,
        checkpoint_backend="memory",
        openai_api_key="sk-openai-stale",
    )
    # Default is grok -> anthropic key from profile plaintext.
    assert settings.active_model == "grok"
    assert settings.anthropic_api_key == "sk-local-test-key"

    reg = registry_from_settings(settings)
    with patch("synapse.models.registry.init_chat_model") as init_mock:
        init_mock.return_value = MagicMock(name="chat")
        build_model_from_settings(settings, model_name="grok")
        kwargs = init_mock.call_args.kwargs
        assert kwargs["api_key"] == "sk-local-test-key"
        assert kwargs.get("anthropic_api_key") == "sk-local-test-key"
        assert kwargs.get("base_url") == "http://localhost:8317"

    # Switch deep -> grok via apply_profile_to_settings and ensure key flips.
    from synapse.models_registry import apply_profile_to_settings

    apply_profile_to_settings(settings, reg.get("deep"), seed_thinking=True)
    assert settings.openai_api_key == "sk-openai-profile"
    apply_profile_to_settings(settings, reg.get("grok"), seed_thinking=True)
    assert settings.anthropic_api_key == "sk-local-test-key"
    with patch("synapse.models.registry.init_chat_model") as init_mock:
        init_mock.return_value = MagicMock(name="chat")
        build_model_from_settings(settings, model_name="grok")
        kwargs = init_mock.call_args.kwargs
        assert kwargs["api_key"] == "sk-local-test-key"


def test_stream_chunk_timeout_disabled_by_default_and_profile_override(
    tmp_path: Path, monkeypatch
) -> None:
    """Avoid langchain-openai 120s StreamChunkTimeoutError on long reasoning."""
    monkeypatch.delenv("AGENT_MODELS_CONFIG", raising=False)
    monkeypatch.delenv("STREAM_CHUNK_TIMEOUT", raising=False)
    # Assertions inspect kwargs passed to init_chat_model (the langchain_openai
    # fallback); disable the native Rust transport here.
    monkeypatch.setenv("SYNAPSE_DISABLE_RUST_OPENAI", "1")
    monkeypatch.setattr(
        "synapse.settings.config_paths.user_config_dir",
        lambda: tmp_path / "nouser" / ".synapse",
    )
    monkeypatch.setattr("synapse.settings.config_paths.executable_config_dirs", lambda: [])
    cfg = {
        "default": "main",
        "models": {
            "main": {"model": "openai:demo"},
            "strict": {
                "model": "openai:demo",
                "stream_chunk_timeout": 90,
            },
        },
    }
    path = tmp_path / ".synapse" / "models.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg), encoding="utf-8")

    settings = load_settings(
        workspace=tmp_path,
        models_config_path=None,
        checkpoint_backend="memory",
    )
    assert settings.stream_chunk_timeout is None

    with patch("synapse.models.registry.init_chat_model") as init_mock:
        init_mock.return_value = MagicMock(name="chat")
        build_model_from_settings(settings, model_name="main")
        kwargs = init_mock.call_args.kwargs
        assert "stream_chunk_timeout" in kwargs
        assert kwargs["stream_chunk_timeout"] is None

        init_mock.reset_mock()
        build_model_from_settings(settings, model_name="strict")
        kwargs = init_mock.call_args.kwargs
        assert kwargs["stream_chunk_timeout"] == 90

    settings = settings.model_copy(update={"stream_chunk_timeout": 600.0})
    with patch("synapse.models.registry.init_chat_model") as init_mock:
        init_mock.return_value = MagicMock(name="chat")
        build_model_from_settings(settings, model_name="main")
        kwargs = init_mock.call_args.kwargs
        assert kwargs["stream_chunk_timeout"] == 600.0


def test_format_model_status_and_thinking_token(tmp_path: Path):
    settings = load_settings(
        workspace=tmp_path,
        models_config_path=None,
        model="openai:deepseek-v4-pro",
        enable_thinking=True,
        reasoning_effort="high",
        checkpoint_backend="memory",
    )
    settings = settings.model_copy(
        update={
            "models_config_path": None,
            "model": "openai:deepseek-v4-pro",
            "enable_thinking": True,
            "reasoning_effort": "high",
        }
    )
    assert settings_thinking_label(settings) == "high"
    assert format_model_status(settings) == "deepseek-v4-pro · high"
    settings = settings.model_copy(update={"enable_thinking": False})
    assert format_model_status(settings) == "deepseek-v4-pro · off"
    assert is_thinking_token("max")
    assert is_thinking_token("off")
    assert not is_thinking_token("primary")


def test_session_store_crud(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions.sqlite")
    from synapse.goals.store import GoalStore

    goals = GoalStore(tmp_path / "sessions.sqlite")
    store.ensure("abc123", title="session abc123", model="openai:x")
    goals.insert("abc123", "remove with session")
    store.touch("abc123", title_hint="Fix the bug in auth", model="openai:x")
    info = store.get("abc123")
    assert info is not None
    assert info.thread_id == "abc123"
    assert "Fix" in (info.title or "") or info.title
    store.rename("abc123", "Renamed session")
    assert store.get("abc123").title == "Renamed session"
    store.delete("abc123")
    assert store.get("abc123") is None
    assert goals.get("abc123") is None
    goals.close()


def test_session_prune_empty_and_resume_last(tmp_path: Path):
    from synapse.sessions import pick_startup_thread_id

    store = SessionStore(tmp_path / "sessions.sqlite")
    store.ensure("empty1", title="session empty1")
    store.ensure("empty2", title="session empty2")
    store.ensure("used1", title="session used1")
    store.touch("used1", title_hint="实现登录功能")
    assert len(store.list()) == 3

    deleted = store.prune_empty()
    assert set(deleted) == {"empty1", "empty2"}
    assert store.get("used1") is not None
    assert store.list_nonempty()[0].thread_id == "used1"

    tid, resumed = pick_startup_thread_id(store, None, resume_last=True)
    assert resumed is True
    assert tid == "used1"

    tid2, resumed2 = pick_startup_thread_id(store, None, resume_last=False)
    assert resumed2 is False
    assert tid2 != "used1"
    assert store.get(tid2) is None  # not persisted until first message


def test_prune_keeps_placeholder_with_binding_and_startup_restores_it(tmp_path: Path):
    """A placeholder session the user picked a model for survives prune and is
    restored on startup (binding wins over the last-used fallback)."""
    from synapse.sessions import pick_startup_thread_id

    store = SessionStore(tmp_path / "sessions.sqlite")
    store.save_model_binding(
        "picked", ModelBinding(active_model="deep", thinking="max"), also_last=False
    )
    store.ensure("empty3", title="session empty3")

    deleted = store.prune_empty()
    assert deleted == ["empty3"]
    assert store.get("picked") is not None  # placeholder with binding survives

    # No real session exists, but the picked placeholder is resumed so its
    # binding is applied instead of falling back to last.
    tid, resumed = pick_startup_thread_id(store, None, resume_last=True)
    assert resumed is True
    assert tid == "picked"
    resolved = resolve_startup_binding(store, thread_id=tid, cli_model=None)
    assert resolved is not None
    assert resolved.active_model == "deep"


def test_session_model_binding_roundtrip(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions.sqlite")
    bind = ModelBinding(
        active_model="deep",
        model="openai:deepseek-v4-pro",
        thinking="max",
    )
    store.save_model_binding("t1", bind, also_last=True)

    got = store.get_model_binding("t1")
    assert got.active_model == "deep"
    assert got.model == "openai:deepseek-v4-pro"
    assert got.thinking == "max"
    assert got.display() == "deep · max"

    last = store.get_last_model_binding()
    assert last.active_model == "deep"
    assert last.thinking == "max"

    resolved = resolve_startup_binding(store, thread_id="t1", cli_model=None)
    assert resolved is not None
    assert resolved.thinking == "max"

    assert resolve_startup_binding(store, thread_id="t1", cli_model="primary") is None

    resolved2 = resolve_startup_binding(store, thread_id="missing", cli_model=None)
    assert resolved2 is not None
    assert resolved2.active_model == "deep"

    settings = load_settings(
        workspace=tmp_path,
        models_config_path=None,
        checkpoint_backend="memory",
        model="openai:other",
        enable_thinking=True,
        reasoning_effort="low",
    )
    apply_binding_to_settings(
        settings,
        ModelBinding(
            active_model=None,
            model="openai:deepseek-v4-pro",
            thinking="max",
        ),
    )
    assert settings.model == "openai:deepseek-v4-pro"
    assert settings.reasoning_effort == "max"
    snap = binding_from_settings(settings)
    assert snap.thinking == "max"


def test_persist_binding_on_exit_skips_inflight_and_writes_row(tmp_path: Path):
    """Exit-time fallback: skip while a switch is in flight / no thread, and
    persist the in-memory binding to the session row otherwise."""
    from synapse.sessions import persist_binding_on_exit

    store = SessionStore(tmp_path / "sessions.sqlite")
    settings = load_settings(
        workspace=tmp_path,
        models_config_path=None,
        checkpoint_backend="memory",
        model="openai:deepseek-v4-flash",
        enable_thinking=True,
        reasoning_effort="high",
    )
    settings.active_model = "deep"  # type: ignore[attr-defined]

    # A switch still settling must never clobber a freshly persisted binding.
    assert persist_binding_on_exit(settings, "t1", store=store, in_flight=True) is False
    assert store.get_model_binding("t1").has_data() is False

    # No current session -> nothing to write.
    assert persist_binding_on_exit(settings, None, store=store) is False

    # Normal exit: the in-memory choice lands on the session row.
    assert persist_binding_on_exit(settings, "t1", store=store) is True
    bind = store.get_model_binding("t1")
    assert bind.active_model == "deep"
    assert bind.model == "openai:deepseek-v4-flash"
    assert bind.thinking == "max"

    # A stored binding that differs from the in-memory one is treated as
    # newer (background switch persisted, settings not yet committed): the
    # exit fallback must never overwrite it with stale settings.
    settings.active_model = "zen"  # type: ignore[attr-defined]  # stale
    assert persist_binding_on_exit(settings, "t1", store=store) is False
    assert store.get_model_binding("t1").active_model == "deep"

    # Back in sync: writing again is a harmless no-op (returns True).
    settings.active_model = "deep"  # type: ignore[attr-defined]
    assert persist_binding_on_exit(settings, "t1", store=store) is True
    assert store.get_model_binding("t1").active_model == "deep"


def test_session_title_from_first_message_and_resolve(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions.sqlite")
    store.ensure("abc123de0001", model="openai:x")
    info = store.get("abc123de0001")
    assert info is not None
    assert info.title.startswith("session ")

    store.touch("abc123de0001", title_hint="  Fix the auth bug in login  ")
    info = store.get("abc123de0001")
    assert info is not None
    assert info.title == "Fix the auth bug in login"

    # Second message should not overwrite bound title.
    store.touch("abc123de0001", title_hint="something else")
    assert store.get("abc123de0001").title == "Fix the auth bug in login"

    hit = store.resolve_session_ref("Fix the auth")
    assert hit is not None
    assert hit.thread_id == "abc123de0001"
    hit2 = store.resolve_session_ref("abc123")
    assert hit2 is not None
    assert hit2.thread_id == "abc123de0001"


def test_format_session_table(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions.sqlite")
    store.ensure("t1", title="one", model="openai:a")
    store.ensure("t2", title="two", model="openai:b")
    table = format_session_table(store.list(limit=10))
    assert "t1" in table or "one" in table
    assert isinstance(table, str)
    assert table.strip()


def test_mcp_config_basic(tmp_path: Path):
    path = tmp_path / "mcp.json"
    path.write_text(
        json.dumps(
            {
                "servers": [
                    {
                        "name": "s1",
                        "transport": "streamable_http",
                        "url": "https://example.com/mcp",
                        "headers": {"Authorization": "Bearer ${TOKEN}"},
                        "enabled": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    servers = load_mcp_server_configs(path=path)
    assert len(servers) == 1
    assert servers[0].name == "s1"
    assert servers[0].transport == "streamable_http"


def test_fs_permissions_and_harness(tmp_path: Path):
    del tmp_path
    # LocalShellBackend: permissions disabled by default (deepagents constraint).
    assert (
        build_filesystem_permissions(
            enabled=True,
            readonly=True,
            deny_paths=["/secrets/**"],
        )
        is None
    )
    perms = build_filesystem_permissions(
        enabled=True,
        readonly=True,
        deny_paths=["/secrets/**"],
        force=True,
        shell_backend=False,
    )
    assert perms is not None
    excluded = apply_harness_exclusions("openai:demo")
    assert "ls" in excluded
    assert "glob" in excluded
    assert "grep" in excluded
    excluded = apply_harness_exclusions("openai:demo", readonly=True)
    assert "write_file" in excluded
    assert "execute" in excluded

    from deepagents.profiles.harness.harness_profiles import _get_harness_profile

    profile = _get_harness_profile("openai:demo")
    assert profile is not None
    assert profile.excluded_tools == frozenset()


def test_default_subagents_optional_models(tmp_path: Path):
    del tmp_path
    subs = build_default_subagents(enabled=True, tester_model="openai:t")
    assert isinstance(subs, list)
    assert len(subs) >= 1
    names = {s.get("name") for s in subs}
    assert "tester" in names


def test_rust_transport_used_when_native_available(tmp_path: Path, monkeypatch):
    """Plain HTTP OpenAI profiles prefer the native Rust transport when present."""
    monkeypatch.delenv("AGENT_MODELS_CONFIG", raising=False)
    monkeypatch.delenv("MODEL", raising=False)
    cfg = {
        "default": "main",
        "models": {
            "main": {
                "model": "openai:gpt-test",
                "api_key": "test-key",
                "base_url": "http://127.0.0.1:9/v1/",
                "temperature": 0.2,
                "max_tokens": 512,
                "model_kwargs": {"foo": 1},
                "extra_body": {"bar": 2},
                "stream_usage": False,
                "thinking": "high",
            },
        },
    }
    path = tmp_path / ".synapse" / "models.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg), encoding="utf-8")
    settings = load_settings(
        workspace=tmp_path,
        models_config_path=None,
        checkpoint_backend="memory",
    )
    reg = registry_from_settings(settings)

    fake_rust_model = MagicMock(name="rust-chat-model")
    with (
        patch("synapse.models.rust_openai.rust_openai_available", return_value=True),
        patch(
            "synapse.models.rust_openai.RustOpenAIChatModel",
            return_value=fake_rust_model,
        ) as rust_model,
    ):
        built = reg.build_chat_model("main", fallback_api_key="k")

    assert built is fake_rust_model
    assert rust_model.call_args.kwargs["model"] == "gpt-test"
    assert rust_model.call_args.kwargs["api_key"] == "test-key"
    assert rust_model.call_args.kwargs["base_url"] == "http://127.0.0.1:9/v1"
    assert rust_model.call_args.kwargs["temperature"] == 0.2
    assert rust_model.call_args.kwargs["max_tokens"] == 512
    assert rust_model.call_args.kwargs["model_kwargs"]["foo"] == 1
    assert rust_model.call_args.kwargs["extra_body"]["bar"] == 2
    assert rust_model.call_args.kwargs["extra_body"]["thinking"]["type"] == "enabled"
    assert rust_model.call_args.kwargs["reasoning_effort"] == "high"
    assert rust_model.call_args.kwargs["stream_usage"] is False
    assert fake_rust_model._coding_rust_openai is True


def test_rust_transport_used_with_proxy(tmp_path: Path, monkeypatch):
    """A proxy URL no longer forces the langchain_openai fallback."""
    monkeypatch.delenv("AGENT_MODELS_CONFIG", raising=False)
    monkeypatch.delenv("MODEL", raising=False)
    cfg = {
        "default": "main",
        "models": {
            "main": {
                "model": "openai:gpt-test",
                "base_url": "http://127.0.0.1:9/v1/",
                "openai_proxy": "socks5://localhost:7991",
                "thinking": "off",
            },
        },
    }
    path = tmp_path / ".synapse" / "models.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg), encoding="utf-8")
    settings = load_settings(
        workspace=tmp_path,
        models_config_path=None,
        checkpoint_backend="memory",
    )

    from synapse.models.registry import should_use_rust_openai

    fake_rust_model = MagicMock(name="rust-chat-model")
    with (
        patch("synapse.models.rust_openai.rust_openai_available", return_value=True),
        patch(
            "synapse.models.rust_openai.RustOpenAIChatModel",
            return_value=fake_rust_model,
        ) as rust_model,
    ):
        reg = registry_from_settings(settings)
        built = reg.build_chat_model("main", fallback_api_key="k")
        assert should_use_rust_openai(settings) is True

    assert built is fake_rust_model
    assert rust_model.call_args.kwargs["proxy"] == "socks5://localhost:7991"
    assert fake_rust_model._coding_rust_openai is True


def test_rust_transport_falls_back_to_langchain_when_native_missing(
    tmp_path: Path, monkeypatch
):
    """Without the native extension, openai: profiles use langchain_openai."""
    monkeypatch.delenv("AGENT_MODELS_CONFIG", raising=False)
    monkeypatch.delenv("MODEL", raising=False)
    cfg = {
        "default": "main",
        "models": {
            "main": {
                "model": "openai:gpt-test",
                "base_url": "http://127.0.0.1:9/v1/",
                "thinking": "off",
            },
        },
    }
    path = tmp_path / ".synapse" / "models.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg), encoding="utf-8")
    settings = load_settings(
        workspace=tmp_path,
        models_config_path=None,
        checkpoint_backend="memory",
    )
    reg = registry_from_settings(settings)

    with (
        patch("synapse.models.rust_openai.rust_openai_available", return_value=False),
        patch("synapse.models.registry.init_chat_model") as init_mock,
    ):
        init_mock.return_value = MagicMock(name="chat")
        built = reg.build_chat_model("main", fallback_api_key="k")

    assert built is init_mock.return_value
    kwargs = init_mock.call_args.kwargs
    assert init_mock.call_args.args == ("openai:gpt-test",)
    assert kwargs["base_url"] == "http://127.0.0.1:9/v1"
    assert kwargs["extra_body"]["thinking"]["type"] == "disabled"