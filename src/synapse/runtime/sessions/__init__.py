"""Session-owned execution lifecycle and event replay.

The public names are re-exported lazily (PEP 562): importing a lightweight
submodule such as :mod:`synapse.runtime.sessions.ref` must not drag in the
execution stack (:mod:`synapse.runtime.sessions.runtime` and
:mod:`synapse.runtime.sessions.manager`).  Accessing a name from this package
still resolves to the same object as importing it from its owning submodule.
"""

from __future__ import annotations

from importlib import import_module
from importlib.util import find_spec

__all__ = [
    "ACTIVE_SESSION_STATUSES",
    "InvalidEventCursorError",
    "NoActiveTurnError",
    "ProjectSharedResources",
    "RuntimeClosedError",
    "RuntimeManager",
    "SessionBusyError",
    "SessionEventBroker",
    "SessionEventEnvelope",
    "SessionEventWindow",
    "SessionPersistence",
    "SessionRef",
    "SessionResolutionError",
    "SessionRuntime",
    "ExecutionBinding",
    "SessionSnapshot",
    "SessionStatus",
    "SessionSubscription",
    "SessionUsage",
    "SteeringUnavailableError",
    "TurnMismatchError",
    "TurnReservation",
    "UserTurn",
    "build_session_agent_factory",
    "parse_global_id",
    "resolve_session_ref",
]

#: Public name -> owning submodule (without the package prefix).
_LAZY_EXPORTS = {
    "InvalidEventCursorError": "errors",
    "NoActiveTurnError": "errors",
    "RuntimeClosedError": "errors",
    "SessionBusyError": "errors",
    "SteeringUnavailableError": "errors",
    "TurnMismatchError": "errors",
    "SessionEventBroker": "events",
    "SessionEventEnvelope": "events",
    "SessionEventWindow": "events",
    "SessionSubscription": "events",
    "ProjectSharedResources": "manager",
    "RuntimeManager": "manager",
    "build_session_agent_factory": "manager",
    "SessionPersistence": "persistence",
    "SessionRef": "ref",
    "SessionResolutionError": "ref",
    "parse_global_id": "ref",
    "resolve_session_ref": "ref",
    "ACTIVE_SESSION_STATUSES": "runtime",
    "ExecutionBinding": "runtime",
    "SessionRuntime": "runtime",
    "SessionSnapshot": "runtime",
    "SessionStatus": "runtime",
    "SessionUsage": "runtime",
    "TurnReservation": "runtime",
    "UserTurn": "runtime",
}


def __getattr__(name: str) -> object:
    """Resolve one public re-export on first access (PEP 562)."""
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is not None:
        value = getattr(import_module(f"{__name__}.{module_name}"), name)
        globals()[name] = value
        return value
    # Preserve pre-lazy behavior for ``package.submodule`` attribute access:
    # a real submodule resolves on demand, anything else is an AttributeError.
    if name.isidentifier() and not name.startswith("_") and find_spec(f"{__name__}.{name}"):
        return import_module(f"{__name__}.{name}")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)
