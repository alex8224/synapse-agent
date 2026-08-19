"""context_window profile field -> model.profile max_input_tokens."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from deepagents.middleware.summarization import compute_summarization_defaults

from synapse.models_registry import (
    ModelProfile,
    ModelRegistry,
    _profiles_from_mapping,
    apply_context_window_to_model,
    merge_model_profiles,
    parse_context_window,
)


def test_parse_context_window_aliases() -> None:
    assert parse_context_window({"context_window": 128000}) == 128000
    assert parse_context_window({"contextwindow": "64000"}) == 64000
    assert parse_context_window({"max_input_tokens": 200000}) == 200000
    assert parse_context_window({"context_window": 0}) is None
    assert parse_context_window({"context_window": "nope"}) is None
    assert parse_context_window({}) is None


def test_profiles_from_mapping_reads_context_window() -> None:
    reg = _profiles_from_mapping(
        {
            "default": "m",
            "models": {
                "m": {
                    "model": "openai:gpt-test",
                    "contextwindow": 96000,
                    "temperature": 0.1,
                }
            },
        }
    )
    prof = reg.get("m")
    assert prof.context_window == 96000
    assert "contextwindow" not in prof.extra
    assert "temperature" in prof.extra


def test_global_and_profile_headers_merge_with_profile_override(monkeypatch) -> None:
    # Assertions inspect kwargs passed to init_chat_model (langchain_openai
    # fallback); disable the native Rust transport here.
    monkeypatch.setenv("SYNAPSE_DISABLE_RUST_OPENAI", "1")
    reg = _profiles_from_mapping(
        {
            "default": "main",
            "headers": {"User-Agent": "global/1", "X-Global": "one"},
            "models": {
                "main": {
                    "model": "openai:test",
                    "headers": {"user-agent": "model/1", "X-Model": "two"},
                }
            },
        }
    )
    model = SimpleNamespace(profile=None)
    with (
        patch(
            "synapse.integrations.http_clients.build_openai_async_http_client",
            return_value=object(),
        ),
        patch("synapse.models.registry.init_chat_model", return_value=model) as init,
    ):
        reg.build_chat_model("main", fallback_api_key="key")

    _, kwargs = init.call_args
    assert kwargs["default_headers"] == {
        "user-agent": "model/1",
        "X-Global": "one",
        "X-Model": "two",
    }


def test_profiles_from_mapping_reads_openai_oauth_auth() -> None:
    reg = _profiles_from_mapping(
        {"default": "codex", "models": {"codex": {"model": "openai:gpt-5", "auth": "OpenAI_OAuth"}}}
    )
    assert reg.get("codex").auth == "openai_oauth"


def test_oauth_profile_uses_codex_backend_and_account_header() -> None:
    reg = ModelRegistry(
        profiles={
            "codex": ModelProfile(
                name="codex",
                model="openai:gpt-5",
                auth="openai_oauth",
                extra={"extra_body": {"thinking": {"type": "enabled"}, "keep": "extra"}},
                extra_body={"thinking": {"type": "disabled"}, "keep_profile": True},
            )
        },
        default="codex",
    )
    model = SimpleNamespace(profile=None)
    provider = SimpleNamespace(access_token=lambda: "oauth-access", account_id=lambda: "acct-123")

    with (
        patch("synapse.integrations.openai_oauth.OpenAIOAuthTokenProvider", return_value=provider),
        patch(
            "synapse.integrations.http_clients.build_openai_async_http_client",
            return_value=object(),
        ),
        patch("synapse.models.registry.init_chat_model", return_value=model) as init,
    ):
        reg.build_chat_model("codex")

    _, kwargs = init.call_args
    assert kwargs["base_url"] == "https://chatgpt.com/backend-api/codex"
    assert kwargs["default_headers"]["ChatGPT-Account-Id"] == "acct-123"
    assert kwargs["default_headers"]["originator"] == "synapse"
    assert kwargs["use_responses_api"] is True
    assert kwargs["store"] is False
    assert kwargs["extra_body"] == {"keep": "extra", "keep_profile": True}
    assert kwargs["reasoning_effort"] == "high"
    assert model._synapse_openai_oauth is True


def test_oauth_profile_requires_chatgpt_account_id() -> None:
    reg = ModelRegistry(
        profiles={
            "codex": ModelProfile(name="codex", model="openai:gpt-5", auth="openai_oauth")
        },
        default="codex",
    )
    provider = SimpleNamespace(access_token=lambda: "oauth-access", account_id=lambda: None)

    with patch("synapse.integrations.openai_oauth.OpenAIOAuthTokenProvider", return_value=provider):
        with pytest.raises(ValueError, match="ChatGPT-Account-Id"):
            reg.build_chat_model("codex")


def test_oauth_profile_ignores_configured_base_url() -> None:
    reg = ModelRegistry(
        profiles={
            "codex": ModelProfile(
                name="codex",
                model="openai:gpt-5",
                auth="openai_oauth",
                base_url="https://untrusted.example/v1",
            )
        },
        default="codex",
    )
    model = SimpleNamespace(profile=None)
    provider = SimpleNamespace(access_token=lambda: "oauth-access", account_id=lambda: "acct-123")

    with (
        patch("synapse.integrations.openai_oauth.OpenAIOAuthTokenProvider", return_value=provider),
        patch(
            "synapse.integrations.http_clients.build_openai_async_http_client",
            return_value=object(),
        ),
        patch("synapse.models.registry.init_chat_model", return_value=model) as init,
    ):
        reg.build_chat_model("codex", fallback_base_url="https://also-untrusted.example/v1")

    _, kwargs = init.call_args
    assert kwargs["base_url"] == "https://chatgpt.com/backend-api/codex"


def test_merge_prefers_override_context_window() -> None:
    base = ModelProfile(name="p", model="openai:a", context_window=1000)
    over = ModelProfile(name="p", model="openai:b", context_window=2000)
    merged = merge_model_profiles(base, over)
    assert merged.context_window == 2000
    keep = merge_model_profiles(base, ModelProfile(name="p", model="openai:c"))
    assert keep.context_window == 1000


def test_apply_context_window_sets_profile() -> None:
    model = SimpleNamespace(profile=None)
    apply_context_window_to_model(model, 128000)
    assert model.profile == {"max_input_tokens": 128000}

    model2 = SimpleNamespace(profile={"max_input_tokens": 1, "other": True})
    apply_context_window_to_model(model2, 64000)
    assert model2.profile["max_input_tokens"] == 64000
    assert model2.profile["other"] is True


def test_build_chat_model_stamps_profile_for_summarization(monkeypatch) -> None:
    # The assertion inspects the langchain_openai fallback path; disable the
    # native Rust transport here (Rust path is covered by
    # test_capabilities::test_rust_transport_used_when_native_available).
    monkeypatch.setenv("SYNAPSE_DISABLE_RUST_OPENAI", "1")
    reg = ModelRegistry(
        profiles={
            "main": ModelProfile(
                name="main",
                model="openai:fake",
                context_window=128000,
            )
        },
        default="main",
    )
    fake = SimpleNamespace(profile=None)

    with patch("synapse.models.registry.init_chat_model", return_value=fake):
        out = reg.build_chat_model("main", fallback_api_key="k")

    assert out is fake
    assert out.profile == {"max_input_tokens": 128000}
    assert out._synapse_openai_oauth is False
    defaults = compute_summarization_defaults(out)
    assert defaults["trigger"] == ("fraction", 0.85)
    assert defaults["keep"] == ("fraction", 0.10)
