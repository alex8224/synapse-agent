"""Foreground S8 runtime daemon and its process-lifetime resources.

The implementations live in the submodules; this package re-exports them
*lazily* (PEP 562) rather than eagerly.  Importing any submodule executes this
module first, and ``synapse.web_console.config`` / ``.host`` need exactly one
constant each from ``daemon.config`` / ``daemon.auth`` -- an eager
``from .application import RuntimeDaemon`` here would drag the whole agent stack
(LangChain/LangGraph/deepagents, ~90 MB RSS) into the loopback console host,
which never builds an agent.  ``from synapse.runtime.daemon import run_daemon``
keeps working unchanged; the import just happens on first attribute access.
"""

from __future__ import annotations

from importlib import import_module
from importlib.util import find_spec
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from synapse.runtime.daemon.application import RuntimeDaemon, run_daemon
    from synapse.runtime.daemon.auth import (
        PROJECT_SCOPE_HEADER,
        BearerTokenAuthenticator,
        TokenFileError,
        load_token,
    )
    from synapse.runtime.daemon.config import DaemonConfig
    from synapse.runtime.daemon.lease import DaemonAlreadyRunningError, DaemonLease

#: Public name -> defining submodule (relative).  Kept in sync with ``__all__``.
_LAZY_EXPORTS: dict[str, str] = {
    "RuntimeDaemon": "application",
    "run_daemon": "application",
    "PROJECT_SCOPE_HEADER": "auth",
    "BearerTokenAuthenticator": "auth",
    "TokenFileError": "auth",
    "load_token": "auth",
    "DaemonConfig": "config",
    "DaemonAlreadyRunningError": "lease",
    "DaemonLease": "lease",
}

__all__ = [
    "BearerTokenAuthenticator",
    "DaemonAlreadyRunningError",
    "DaemonConfig",
    "DaemonLease",
    "PROJECT_SCOPE_HEADER",
    "RuntimeDaemon",
    "TokenFileError",
    "load_token",
    "run_daemon",
]


def __getattr__(name: str) -> Any:
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
