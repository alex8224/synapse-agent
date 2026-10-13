"""One session's model/reasoning axes: the single resolution step.

A session's ``active_model`` / ``model`` / ``thinking`` triple (see
:class:`synapse.sessions.store.ModelBinding`) is *session* state, but the app
holds exactly one live ``Settings`` object per foreground session: the TUI
mutates ``app.settings`` in place, and the daemon hands each session runtime a
settings copy.  Every path that opens or switches to a session therefore has to
re-derive that triple from the same inputs.  When a path instead mutates the
object incrementally, the previous session's value survives into the next one,
or a default silently replaces a level the user chose.

:func:`resolve_session_axes` is that single step.  Resolution order, first match
wins:

1. the session's own persisted binding (its model and its reasoning level);
2. the project layer's explicit reasoning default
   (``<workspace>/.synapse/settings.json``);
3. the loaded project settings' own value (model-profile seeded).

The baseline reset (1 before 2) is what makes a session switch a *resolution*
rather than a mutation: a session whose binding carries no level of its own
falls back to the project's defaults instead of inheriting whatever the
previous session left on the live object.
"""

from __future__ import annotations

import logging
from typing import Any

from synapse.models.helpers import apply_thinking_to_settings
from synapse.sessions.store import apply_binding_to_settings

__all__ = [
    "SESSION_AXIS_FIELDS",
    "apply_project_layer_thinking",
    "resolve_session_axes",
    "restore_session_axes",
    "snapshot_session_axes",
]

_LOGGER = logging.getLogger(__name__)

#: Fields that make up a session's model/reasoning identity.  Kept in one place
#: so the daemon and the TUI cannot drift on what a session switch has to move:
#: a field missing from this tuple would survive a switch and silently keep the
#: previous session's value in force.
SESSION_AXIS_FIELDS: tuple[str, ...] = (
    "active_model",
    "model",
    "enable_thinking",
    "reasoning_effort",
    "parallel_tool_calls",
    "openai_api_key",
    "anthropic_api_key",
    "openai_base_url",
)

#: Value used when an object does not carry the field at all: the axis defaults a
#: fresh ``Settings`` would have.  Only partial settings objects (test doubles,
#: adapters) hit these; a real ``Settings`` always has every axis.
_AXIS_DEFAULTS: dict[str, Any] = {
    "enable_thinking": True,
    "parallel_tool_calls": True,
}


def snapshot_session_axes(settings: Any) -> tuple[Any, ...]:
    """Capture the session axes of ``settings`` (see :data:`SESSION_AXIS_FIELDS`).

    Taken from a *pristine* project settings object this is the baseline a
    session switch resolves against, so it has to be captured before any
    session binding is applied to that object.
    """
    return tuple(
        getattr(settings, name, _AXIS_DEFAULTS.get(name)) for name in SESSION_AXIS_FIELDS
    )


def restore_session_axes(settings: Any, snapshot: tuple[Any, ...]) -> bool:
    """Restore a snapshot taken by :func:`snapshot_session_axes`.

    Returns True when at least one axis changed, i.e. when the caller has to
    rebuild the agent graph.  Fields the snapshot does not carry (a shorter
    tuple, or an object that never had the attribute) are left untouched rather
    than cleared.
    """
    changed = False
    for name, value in zip(SESSION_AXIS_FIELDS, snapshot, strict=False):
        if getattr(settings, name, None) != value:
            setattr(settings, name, value)
            changed = True
    return changed


def apply_project_layer_thinking(settings: Any, workspace: Any) -> bool:
    """Seed ``settings`` with the project layer's explicit default level.

    Applied *before* a session's own binding, so a session that chose its own
    level keeps it.  The value has to be read from the project layer because the
    loaded ``Settings`` object already had ``reasoning_effort`` overwritten by
    the selected model profile, so the project layer is the only place that
    still knows the project's own default.

    Returns True when the level changed.  A default that is no longer inside the
    live whitelist (for example after a model switch) is skipped with a warning
    instead of blocking the session open.
    """
    from synapse.runtime.service.config_source import resolve_thinking_levels
    from synapse.settings.config_paths import read_project_thinking_default

    level = read_project_thinking_default(workspace)
    if level is None:
        return False
    before = (
        getattr(settings, "enable_thinking", True),
        getattr(settings, "reasoning_effort", None),
    )
    try:
        allowed = list(resolve_thinking_levels(settings))
        apply_thinking_to_settings(settings, level, allowed=allowed)
    except Exception as exc:  # noqa: BLE001 - a stale default must not block opening
        _LOGGER.warning(
            "ignoring project reasoning default %r for workspace %s: %s",
            level,
            workspace,
            exc,
        )
        return False
    after = (
        getattr(settings, "enable_thinking", True),
        getattr(settings, "reasoning_effort", None),
    )
    return before != after


def resolve_session_axes(
    settings: Any,
    *,
    binding: Any,
    baseline: tuple[Any, ...] | None = None,
    workspace: Any = None,
) -> bool:
    """Resolve one session's model/reasoning axes onto ``settings``.

    ``binding`` is the session's persisted :class:`~synapse.sessions.store.ModelBinding`.
    ``baseline`` is a :func:`snapshot_session_axes` snapshot of the pristine
    project settings (``None`` when the caller has no snapshot, in which case
    the axes are only layered, not reset).  ``workspace`` enables the project
    layer default; without it the caller's own object is the baseline, which is
    what a single-session process (CLI startup) already is.

    Returns True when the *resolved* axes differ from the ones the object came in
    with, i.e. when the caller has to rebuild the agent graph for them to take
    effect.  The comparison is against the entry state, not against each
    intermediate step, so resolving the same session twice reports no change
    even though the reset transiently moved the axes.
    """
    before = snapshot_session_axes(settings)
    if baseline is not None:
        restore_session_axes(settings, baseline)
    if workspace is not None:
        apply_project_layer_thinking(settings, workspace)
    apply_binding_to_settings(settings, binding)
    return snapshot_session_axes(settings) != before
