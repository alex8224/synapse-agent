"""Foreground S8 runtime daemon and its process-lifetime resources."""

from synapse.runtime.daemon.application import RuntimeDaemon, run_daemon
from synapse.runtime.daemon.auth import BearerTokenAuthenticator, TokenFileError, load_token
from synapse.runtime.daemon.config import DaemonConfig
from synapse.runtime.daemon.lease import DaemonAlreadyRunningError, DaemonLease

__all__ = [
    "BearerTokenAuthenticator",
    "DaemonAlreadyRunningError",
    "DaemonConfig",
    "DaemonLease",
    "RuntimeDaemon",
    "TokenFileError",
    "load_token",
    "run_daemon",
]
