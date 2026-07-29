"""Settings, capability, and presentation helpers for model profiles."""
from __future__ import annotations

from typing import Any

from synapse.models.config import (
    THINKING_LEVELS,
    default_thinking_levels,
    normalize_thinking_level,
)
from synapse.models.profile import ModelProfile


def model_provider(model: str | None) -> str:
    """Return provider prefix (openai/anthropic/...) or empty string."""
    text = (model or "").strip()
    if ":" not in text:
        return ""
    return text.split(":", 1)[0].strip().casefold()


def model_supports_image_input(
    model: str | None,
    explicit: bool | None = None,
    base_url: str | None = None,
) -> bool:
    """Resolve native image support with explicit and endpoint-aware defaults.

    Official model names are not enough to prove that a custom OpenAI-compatible
    gateway accepts image blocks. Non-official OpenAI endpoints therefore default
    to text-only; set ``image_input: true`` to opt in explicitly.
    """
    if explicit is not None:
        return bool(explicit)
    raw = (model or "").strip().casefold()
    provider, _, model_id = raw.partition(":")
    name = model_id or raw
    if provider in {"anthropic", "claude"}:
        return "claude-3" in name or "claude-4" in name or "sonnet-4" in name or "opus-4" in name
    if provider in {"google", "google_genai", "google_vertexai", "gemini", "vertexai"}:
        return "gemini" in name
    if provider in {"openai", "azure_openai", "azure"} or not provider:
        if base_url and not _is_official_openai_endpoint(base_url):
            return False
        return any(
            marker in name
            for marker in (
                "gpt-4o",
                "gpt-4.1",
                "gpt-4-turbo",
                "gpt-5",
                "gpt5",
                "o3",
                "o4",
            )
        )
    return False


def _is_official_openai_endpoint(base_url: str) -> bool:
    normalized = base_url.strip().casefold().rstrip("/")
    return normalized in {
        "https://api.openai.com",
        "https://api.openai.com/v1",
        "https://chatgpt.com/backend-api/codex",
    }


def settings_fallback_api_key(settings: Any, model: str | None = None) -> str | None:
    """Pick settings-level API key for a model provider.

    Profile plaintext / api_key_env always wins earlier via resolved_api_key().
    This only supplies the fallback when the profile has no key.
    """
    provider = model_provider(model or getattr(settings, "model", None))
    openai_key = getattr(settings, "openai_api_key", None)
    anthropic_key = getattr(settings, "anthropic_api_key", None)
    if provider == "anthropic":
        return anthropic_key
    if provider == "openai":
        return openai_key
    return openai_key or anthropic_key


def apply_profile_to_settings(
    settings: Any,
    profile: ModelProfile,
    *,
    seed_thinking: bool = True,
) -> Any:
    """Apply a model profile's identity + credentials onto Settings.

    Thinking defaults are seeded only when ``seed_thinking`` is True (model switch).
    Credentials always follow the selected profile so switching models cannot keep
    the previous provider's key as the active credential source.
    """
    settings.active_model = profile.name
    settings.model = profile.model
    if profile.base_url:
        # Shared transport field used as OpenAI-compatible / Anthropic base_url source.
        settings.openai_base_url = profile.base_url

    key = profile.resolved_api_key()
    provider = model_provider(profile.model)
    if key:
        if provider == "anthropic":
            settings.anthropic_api_key = key
            settings.openai_api_key = None
        else:
            # openai + generic OpenAI-compatible providers
            settings.openai_api_key = key
            settings.anthropic_api_key = None
    else:
        # Clear both so stale keys from a previous profile cannot leak.
        settings.openai_api_key = None
        settings.anthropic_api_key = None

    if seed_thinking:
        if profile.enable_thinking is not None:
            settings.enable_thinking = bool(profile.enable_thinking)
        if profile.reasoning_effort:
            settings.reasoning_effort = profile.reasoning_effort
        if profile.parallel_tool_calls is not None:
            settings.parallel_tool_calls = bool(profile.parallel_tool_calls)
    return settings

def short_model_id(model: str | None) -> str:
    """Strip provider prefix: ``openai:deepseek-v4-pro`` -> ``deepseek-v4-pro``."""
    text = (model or "").strip()
    if ":" in text:
        text = text.split(":", 1)[1]
    return text or "model"


def settings_thinking_label(settings: Any) -> str:
    """Current effective thinking label from Settings (runtime override aware)."""
    if getattr(settings, "enable_thinking", True) is False:
        return "off"
    effort = getattr(settings, "reasoning_effort", None)
    if effort:
        return str(effort)
    return "on"


def format_model_status(settings: Any) -> str:
    """Status-bar label: ``deepseek-v4-pro · high``."""
    model = short_model_id(str(getattr(settings, "model", "") or ""))
    return f"{model} · {settings_thinking_label(settings)}"


def apply_thinking_to_settings(
    settings: Any,
    raw: str,
    *,
    allowed: list[str] | None = None,
) -> str:
    """Apply a thinking level token onto settings. Returns canonical label.

    Accepts off|minimal|low|medium|high|max (and aliases).
    When ``allowed`` is provided, the level must be in that catalog.
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("empty thinking level")
    level = normalize_thinking_level(text)
    if not level:
        raise ValueError(f"unknown thinking level: {raw}")
    catalog = list(allowed) if allowed is not None else default_thinking_levels()
    if level not in catalog:
        raise ValueError(
            f"thinking level {level!r} not allowed; choose one of: {', '.join(catalog)}"
        )
    if level == "off":
        settings.enable_thinking = False
        return "off"
    settings.enable_thinking = True
    settings.reasoning_effort = level
    return level


def is_thinking_token(raw: str) -> bool:
    """True if token looks like a thinking level (for /model parsing)."""
    text = (raw or "").strip().casefold()
    if text in {
        "off",
        "false",
        "0",
        "disabled",
        "none",
        "no",
        "on",
        "true",
        "1",
        "enabled",
        "yes",
        *THINKING_LEVELS,
        "min",
        "med",
        "mid",
        "maximum",
        "xhigh",
        "ultra",
        "highest",
    }:
        return True
    return normalize_thinking_level(text) in THINKING_LEVELS
