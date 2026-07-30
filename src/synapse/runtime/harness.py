"""Resolve model-facing tool exclusions for the coding harness."""

from __future__ import annotations

from collections.abc import Iterable

_DEFAULT_EXCLUDES = frozenset({"ls", "glob", "grep"})
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
    """Return the tools that should be hidden from model requests.

    ``ls`` is excluded by default because ``execute`` can run the project's
    native directory commands. Read-only mode additionally excludes ``execute``,
    ``write_file``, and ``edit_file``.

    ``model_spec`` is used only to disable deepagents' built-in general-purpose
    subagent. Tool exclusions are applied by Synapse request middleware instead
    of deepagents' process-global harness registry because that registry unions
    exclusions and cannot re-enable a tool such as ``grep`` later in the same
    process.
    """
    names = set(_DEFAULT_EXCLUDES)
    names.update(excluded_tools or [])
    if readonly:
        names |= set(_DEFAULT_READONLY_EXCLUDES)
    excluded = frozenset(n.strip() for n in names if n and n.strip())

    from deepagents import (
        GeneralPurposeSubagentProfile,
        HarnessProfile,
        register_harness_profile,
    )

    # Tool exclusions are request-local middleware in ``build_coding_agent``.
    # Do not register them here: deepagents merges profile exclusions globally,
    # so a readonly agent would otherwise make later writable agents readonly too.
    profile = HarnessProfile(
        general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
    )
    register_harness_profile(model_spec, profile)
    if ":" in model_spec:
        provider = model_spec.split(":", 1)[0]
        if provider:
            register_harness_profile(provider, profile)
    return excluded
