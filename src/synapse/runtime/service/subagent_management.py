"""Request/result DTOs for the subagent model/reasoning configuration surface.

Pure data only: no implementation, transport, settings, or framework import.  The
``REASONING_EFFORT_LEVELS`` vocabulary is imported from
``synapse.runtime.subagent_specs`` (the single source of truth for the levels),
which is a light facade, so this module stays import-pure.

A role's *effective* model/reasoning is what the subagent will actually use
(with the main agent's value when the axis resolves to ``None``), while the
``*_override`` field is the explicit per-role override the caller may clear.
``None`` on either axis means "inherit".
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from synapse.runtime.sessions.ref import SessionRef

__all__ = [
    "GetSubagentConfigQuery",
    "SetSubagentConfigCommand",
    "SubagentConfigView",
    "SubagentRoleView",
]

#: ``"inherit"`` is accepted wherever a level is required, meaning "skip this
#: layer" (identical to ``None``).  The vocabulary itself is owned by
#: ``subagent_specs`` so the wire can never drift from the agent assembly.
_INHERIT = "inherit"


def _reasoning_effort_levels() -> tuple[str, ...]:
    from synapse.runtime.subagent_specs import REASONING_EFFORT_LEVELS

    return REASONING_EFFORT_LEVELS


def _validate_level(value: object, field: str) -> None:
    if type(value) is not str or (
        value != _INHERIT and value not in _reasoning_effort_levels()
    ):
        raise ValueError(
            f"{field} must be one of {', '.join(_reasoning_effort_levels())} or 'inherit'"
        )


def _validate_text(value: object, field: str) -> None:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")


def _validate_level_map(value: object, field: str) -> Mapping[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be a mapping")
    for key, item in value.items():
        if type(key) is not str or not key.strip():
            raise ValueError(f"{field} keys must be non-empty strings")
        _validate_level(item, f"{field}[{key!r}]")
    return value


def _validate_text_map(value: object, field: str) -> Mapping[str, str]:
    """Validate a ``{role: value}`` map of non-empty strings (no level check)."""
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be a mapping")
    for key, item in value.items():
        if type(key) is not str or not key.strip():
            raise ValueError(f"{field} keys must be non-empty strings")
        _validate_text(item, f"{field}[{key!r}]")
    return value


@dataclass(frozen=True, slots=True)
class SubagentRoleView:
    """One subagent role's effective and explicit model/reasoning values.

    ``model`` / ``reasoning_effort`` are the effective values (``None`` means
    the role inherits the main agent's value).  ``model_override`` /
    ``reasoning_override`` are the explicit per-role overrides persisted in the
    configuration (``None`` means no override is set for this role).
    """

    name: str
    description: str
    model: str | None
    reasoning_effort: str | None
    model_override: str | None
    reasoning_override: str | None


@dataclass(frozen=True, slots=True)
class GetSubagentConfigQuery:
    """Read the current subagent model/reasoning configuration."""

    session: SessionRef


@dataclass(frozen=True, slots=True)
class SetSubagentConfigCommand:
    """Replace the subagent model/reasoning configuration.

    Every field is optional; an absent field clears that axis (subject to the
    store's own merge rules).  Reasoning values must be a
    ``REASONING_EFFORT_LEVELS`` member or the literal string ``"inherit"``.
    """

    session: SessionRef
    default_model: str | None = None
    default_reasoning_effort: str | None = None
    model_overrides: Mapping[str, str] | None = None
    reasoning_overrides: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        if self.default_model is not None:
            _validate_text(self.default_model, "default_model")
        if self.default_reasoning_effort is not None:
            _validate_level(self.default_reasoning_effort, "default_reasoning_effort")
        if self.model_overrides is not None:
            _validate_text_map(self.model_overrides, "model_overrides")
        if self.reasoning_overrides is not None:
            _validate_level_map(self.reasoning_overrides, "reasoning_overrides")


@dataclass(frozen=True, slots=True)
class SubagentConfigView:
    """The effective subagent configuration plus the enumerable roles.

    ``default_model`` / ``default_reasoning_effort`` are the layer defaults
    (``None`` means "inherit").  ``model_overrides`` /
    ``reasoning_effort_overrides`` are the persisted per-role overrides.
    ``reasoning_levels`` is the shared allowed-level vocabulary, and ``roles``
    lists every registered role with its effective and explicit values.
    """

    default_model: str | None
    default_reasoning_effort: str | None
    model_overrides: Mapping[str, str]
    reasoning_effort_overrides: Mapping[str, str]
    reasoning_levels: tuple[str, ...]
    roles: tuple[SubagentRoleView, ...]
