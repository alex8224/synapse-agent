"""HarnessProfile registration helpers (excluded_tools etc.)."""

from __future__ import annotations

from collections.abc import Iterable

_DEFAULT_READONLY_EXCLUDES = frozenset(
    {
        "execute",
        "write_file",
        "edit_file",
    }
)


def apply_harness_exclusions(
    model_spec: str,
    *,
    readonly: bool = False,
    excluded_tools: Iterable[str] | None = None,
) -> frozenset[str]:
    """Register harness excluded_tools for the active model/provider.

    deepagents only removes built-in tools via HarnessProfile.excluded_tools
    (tools= is additive). Registration is additive/merge under the same key.
    """
    names = set(excluded_tools or [])
    if readonly:
        names |= set(_DEFAULT_READONLY_EXCLUDES)
    excluded = frozenset(n.strip() for n in names if n and n.strip())
    if not excluded:
        return frozenset()

    from deepagents import HarnessProfile, register_harness_profile

    profile = HarnessProfile(excluded_tools=excluded)
    # Model-level and provider-level keys so both string specs and prebuilt
    # BaseChatModel resolution paths can match.
    register_harness_profile(model_spec, profile)
    if ":" in model_spec:
        provider = model_spec.split(":", 1)[0]
        if provider:
            register_harness_profile(provider, profile)
    return excluded
