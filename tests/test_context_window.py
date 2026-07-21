"""context_window profile field -> model.profile max_input_tokens."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from synapse.models_registry import (
    ModelProfile,
    ModelRegistry,
    apply_context_window_to_model,
    merge_model_profiles,
    parse_context_window,
    _profiles_from_mapping,
)
from deepagents.middleware.summarization import compute_summarization_defaults


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


def test_build_chat_model_stamps_profile_for_summarization() -> None:
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

    with patch("synapse.models_registry.init_chat_model", return_value=fake):
        out = reg.build_chat_model("main", fallback_api_key="k")

    assert out is fake
    assert out.profile == {"max_input_tokens": 128000}
    defaults = compute_summarization_defaults(out)
    assert defaults["trigger"] == ("fraction", 0.85)
    assert defaults["keep"] == ("fraction", 0.10)
