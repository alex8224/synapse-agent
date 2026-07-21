"""Tests for multi-model registry, sessions, MCP config, and agent wiring."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from synapse.config import load_settings
from synapse.fs_permissions import build_filesystem_permissions
from synapse.harness import apply_harness_exclusions
from synapse.mcp_client import load_mcp_server_configs
from synapse.models_registry import (
    apply_thinking_to_settings,
    build_model_from_settings,
    format_model_status,
    is_thinking_token,
    registry_from_settings,
    settings_thinking_label,
)
from synapse.sessions import (
    ModelBinding,
    SessionStore,
    apply_binding_to_settings,
    binding_from_settings,
    format_session_table,
    resolve_startup_binding,
)
from synapse.subagents import build_default_subagents


def test_registry_legacy_single_model(tmp_path: Path, monkeypatch):
    # Isolate from ~/.synapse/models.json layered discovery.
    monkeypatch.setattr(
        "synapse.config_paths.user_config_dir",
        lambda: tmp_path / "nouser" / ".synapse",
    )
    monkeypatch.setattr("synapse.config_paths.executable_config_dirs", lambda: [])
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

    with patch("synapse.models_registry.init_chat_model") as init_mock:
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


def test_thinking_levels_array_and_session_override(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("AGENT_MODELS_CONFIG", raising=False)
    monkeypatch.delenv("MODEL", raising=False)
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
    with patch("synapse.models_registry.init_chat_model") as init_mock:
        init_mock.return_value = MagicMock(name="chat")
        build_model_from_settings(settings, model_name="main")
        kwargs = init_mock.call_args.kwargs
        assert kwargs["reasoning_effort"] == "low"
        assert kwargs["extra_body"]["thinking"]["type"] == "enabled"

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
    with patch("synapse.models_registry.init_chat_model") as init_mock:
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
    with patch("synapse.models_registry.init_chat_model") as init_mock:
        init_mock.return_value = MagicMock(name="chat")
        build_model_from_settings(settings, model_name="grok")
        kwargs = init_mock.call_args.kwargs
        assert kwargs["api_key"] == "sk-local-test-key"


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
    store.ensure("abc123", title="session abc123", model="openai:x")
    store.touch("abc123", title_hint="Fix the bug in auth", model="openai:x")
    info = store.get("abc123")
    assert info is not None
    assert info.thread_id == "abc123"
    assert "Fix" in (info.title or "") or info.title
    store.rename("abc123", "Renamed session")
    assert store.get("abc123").title == "Renamed session"
    store.delete("abc123")
    assert store.get("abc123") is None


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
    excluded = apply_harness_exclusions("openai:demo", readonly=True)
    assert "write_file" in excluded
    assert "execute" in excluded


def test_default_subagents_optional_models(tmp_path: Path):
    del tmp_path
    subs = build_default_subagents(enabled=True, tester_model="openai:t")
    assert isinstance(subs, list)
    assert len(subs) >= 1
    names = {s.get("name") for s in subs}
    assert "tester" in names
