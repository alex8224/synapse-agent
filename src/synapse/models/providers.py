"""Provider catalog for model management.

Only providers that are usable out of the box are listed: the OpenAI-compatible
transport (``openai`` + optional base_url), Anthropic, and the Codex OAuth
endpoint (``openai_oauth``).  Other LangChain providers require uninstalled
integration packages and per-provider build wiring in
``models.registry.build_chat_model``, so they are intentionally excluded from
the catalog until that wiring exists — unknown provider keys map to the
OpenAI-compatible transport during Codex import.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderSpec:
    """One selectable model provider for the form/catalog UI."""

    key: str
    label: str
    default_base_url: str
    default_env_key: str
    wire_api: str
    notes: str = ""
    # True when no extra integration package / auth setup is required.
    available: bool = True


# Canonical provider entries. ``model`` profile values use the ``provider:``
# prefix; ``openai`` doubles as the OpenAI-compatible gateway when a custom
# base_url is supplied.
PROVIDERS: dict[str, ProviderSpec] = {
    "openai": ProviderSpec(
        key="openai",
        label="OpenAI / OpenAI-compatible",
        default_base_url="https://api.openai.com/v1",
        default_env_key="OPENAI_API_KEY",
        wire_api="chat",
        notes="Custom base_url enables any OpenAI-compatible gateway.",
    ),
    "anthropic": ProviderSpec(
        key="anthropic",
        label="Anthropic",
        default_base_url="https://api.anthropic.com",
        default_env_key="ANTHROPIC_API_KEY",
        wire_api="chat",
    ),
    "openai_oauth": ProviderSpec(
        key="openai_oauth",
        label="OpenAI ChatGPT (Codex OAuth)",
        default_base_url="https://chatgpt.com/backend-api/codex",
        default_env_key="",
        wire_api="responses",
        notes="Requires: synapse auth openai login",
    ),
}

# Order for dropdowns / catalog rows.
PROVIDER_ORDER: tuple[str, ...] = ("openai", "anthropic", "openai_oauth")


def known_providers() -> list[ProviderSpec]:
    """Provider specs in catalog order."""
    return [PROVIDERS[key] for key in PROVIDER_ORDER if key in PROVIDERS]


def by_key(key: str | None) -> ProviderSpec | None:
    if not key:
        return None
    return PROVIDERS.get(str(key).strip().casefold())


def normalize_provider(value: str | None) -> str | None:
    """Canonical provider key (case-insensitive) or None."""
    key = str(value or "").strip().casefold() or None
    if key is None:
        return None
    if key in {"openai_compatible", "compatible", "custom", "openai-compatible"}:
        return "openai"
    return key if key in PROVIDERS else "openai"