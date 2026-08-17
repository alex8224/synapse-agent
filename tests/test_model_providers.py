"""Tests for the model provider catalog."""

from __future__ import annotations

from synapse.models.providers import (
    PROVIDERS,
    by_key,
    known_providers,
    normalize_provider,
)


def test_catalog_only_lists_out_of_box_providers() -> None:
    specs = known_providers()
    keys = [spec.key for spec in specs]
    assert keys == ["openai", "anthropic", "openai_oauth"]
    assert all(spec.available for spec in specs)


def test_by_key_normalizes_case() -> None:
    assert by_key("OpenAI") is PROVIDERS["openai"]
    assert by_key("  ANTHROPIC  ") is PROVIDERS["anthropic"]
    assert by_key("nope") is None


def test_normalize_provider_maps_custom_aliases_to_openai() -> None:
    assert normalize_provider("custom") == "openai"
    assert normalize_provider("openai-compatible") == "openai"
    assert normalize_provider("compatible") == "openai"
    assert normalize_provider("OpenAI") == "openai"
    assert normalize_provider(None) is None


def test_openai_provider_doubles_as_compatible_gateway() -> None:
    spec = PROVIDERS["openai"]
    assert spec.wire_api == "chat"
    assert spec.default_env_key == "OPENAI_API_KEY"
