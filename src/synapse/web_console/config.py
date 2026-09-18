"""Configuration for the loopback Synapse Web Console host."""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib.resources import files as package_files
from pathlib import Path

from synapse.runtime.daemon.config import DaemonConfig

_DEFAULT_STATE_DIR = DaemonConfig().state_dir

DEFAULT_PORT = 8080
DEFAULT_MESSAGE_BYTES = 1024 * 1024
DEFAULT_SESSION_TTL_SECONDS = 12 * 60 * 60
DEFAULT_PAIR_TTL_SECONDS = 300
DEFAULT_MAX_BODY_BYTES = 4096
DEFAULT_MAX_CONCURRENT_SOCKETS = 16
DEFAULT_WS_HEARTBEAT_SECONDS = 30
DEFAULT_SEND_TIMEOUT_SECONDS = 30.0
DEFAULT_DAEMON_TIMEOUT_SECONDS = 5.0
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


@dataclass(frozen=True, slots=True)
class WebConsoleConfig:
    """Validated startup parameters for the web console host."""

    workspace: Path
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    static_dir: Path | None = None
    state_dir: Path = _DEFAULT_STATE_DIR
    token_file: Path | None = None
    catalog_path: Path | None = None
    #: Which projects the relay may address.  ``workspace`` keeps the original
    #: single-project boundary; ``all`` widens it to every project registered in
    #: the same user-layer catalog the daemon resolves from (see
    #: ``docs/web-console/formal-host.md`` §4.1).
    project_scope: str = "all"
    runtime_host: str = "127.0.0.1"
    runtime_port: int | None = None
    max_message_bytes: int = DEFAULT_MESSAGE_BYTES
    session_ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS
    pair_ttl_seconds: float = DEFAULT_PAIR_TTL_SECONDS
    #: ``False`` only behind the explicit ``--no-pairing`` opt-in: the host then
    #: mints a session for any same-origin loopback browser instead of asking for
    #: the one-time code.  Local debugging only; never the shipped default.
    pairing_required: bool = True
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES
    max_concurrent_sockets: int = DEFAULT_MAX_CONCURRENT_SOCKETS
    ws_heartbeat_seconds: int = DEFAULT_WS_HEARTBEAT_SECONDS
    send_timeout_seconds: float = DEFAULT_SEND_TIMEOUT_SECONDS
    daemon_timeout_seconds: float = DEFAULT_DAEMON_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if (
            type(self.host) is not str
            or not self.host
            or self.host.strip() != self.host
            or "\x00" in self.host
            or self.host not in LOOPBACK_HOSTS
        ):
            raise ValueError(
                "host must be a loopback address (127.0.0.1, localhost, or ::1); "
                "the web console only supports loopback single-user deployments"
            )
        if type(self.port) is not int or not 0 <= self.port <= 65535:
            raise ValueError("port must be an integer between 0 and 65535")
        workspace = Path(self.workspace).expanduser()
        if not workspace.is_dir():
            raise ValueError(f"workspace is not a directory: {workspace}")
        object.__setattr__(self, "workspace", workspace)
        if type(self.runtime_host) is not str or self.runtime_host not in LOOPBACK_HOSTS:
            raise ValueError(
                "runtime_host must be a loopback address (127.0.0.1, localhost, or ::1); "
                "remote runtime daemons are not supported by this slice"
            )
        if self.runtime_port is not None and (
            type(self.runtime_port) is not int or not 1 <= self.runtime_port <= 65535
        ):
            raise ValueError("runtime_port must be an integer between 1 and 65535")
        if (
            type(self.max_message_bytes) is not int
            or not 1024 <= self.max_message_bytes <= 8 * 1024 * 1024
        ):
            raise ValueError("max_message_bytes must be between 1024 and 8388608")
        if type(self.session_ttl_seconds) is not int or self.session_ttl_seconds <= 0:
            raise ValueError("session_ttl_seconds must be a positive integer")
        if self.project_scope not in ("workspace", "all"):
            raise ValueError("project_scope must be 'workspace' or 'all'")
        if type(self.pair_ttl_seconds) not in (int, float) or self.pair_ttl_seconds <= 0:
            raise ValueError("pair_ttl_seconds must be positive")
        if type(self.pairing_required) is not bool:
            raise ValueError("pairing_required must be a boolean")
        if (
            type(self.max_concurrent_sockets) is not int
            or not 1 <= self.max_concurrent_sockets <= 256
        ):
            raise ValueError("max_concurrent_sockets must be between 1 and 256")
        if type(self.max_body_bytes) is not int or not 64 <= self.max_body_bytes <= 1024 * 1024:
            raise ValueError("max_body_bytes must be between 64 and 1048576")
        if (
            type(self.ws_heartbeat_seconds) is not int
            or not 0 <= self.ws_heartbeat_seconds <= 3600
        ):
            raise ValueError("ws_heartbeat_seconds must be between 0 and 3600")
        if (
            type(self.daemon_timeout_seconds) not in (int, float)
            or self.daemon_timeout_seconds <= 0
        ):
            raise ValueError("daemon_timeout_seconds must be positive")
        if (
            type(self.send_timeout_seconds) not in (int, float)
            or self.send_timeout_seconds <= 0
        ):
            raise ValueError("send_timeout_seconds must be positive")

    @property
    def resolved_token_file(self) -> Path:
        """Daemon token file the host reads server-side (never a query param)."""
        if self.token_file is not None:
            return Path(self.token_file).expanduser()
        return Path(self.state_dir).expanduser() / "token"

    @property
    def resolved_metadata_file(self) -> Path:
        """Daemon discovery file written by ``DaemonLease.publish``."""
        return Path(self.state_dir).expanduser() / "daemon.json"

    def resolved_static_dir(self) -> Path:
        """Resolve explicit, bundled, or source-checkout static assets."""
        if self.static_dir is not None:
            root = Path(self.static_dir).expanduser().resolve()
        else:
            root = _bundled_static_dir() or (self.workspace / "web" / "dist")
        if not root.is_dir():
            raise ValueError(
                f"static build directory not found: {root}; build web/ first or pass "
                "--static-dir"
            )
        if not (root / "index.html").is_file():
            # Fail at startup instead of serving a console that 404s.
            raise ValueError(
                f"static build directory has no index.html: {root}; build web/ first "
                "or pass --static-dir"
            )
        return root


def _bundled_static_dir() -> Path | None:
    """Return the filesystem path of static assets bundled into the wheel.

    Hatch places the built ``web/dist`` contents in this package directory. A
    normal wheel installation exposes package resources as files, which is
    required by aiohttp's ``FileResponse``. Non-filesystem importers are
    deliberately ignored; callers can still provide ``--static-dir``.
    """
    try:
        resource = package_files("synapse.web_console").joinpath("static")
    except (ModuleNotFoundError, OSError):
        return None
    if not resource.is_dir():
        return None
    try:
        root = Path(resource)
    except TypeError:
        return None
    return root.resolve()


class RuntimeDiscoveryError(RuntimeError):
    """The runtime daemon endpoint could not be discovered from metadata."""


def discover_runtime_endpoint(config: WebConsoleConfig) -> tuple[str, int]:
    """Return ``(host, port)`` of the daemon; explicit args win over metadata."""
    if config.runtime_port is not None:
        return config.runtime_host, config.runtime_port
    metadata = config.resolved_metadata_file
    if not metadata.is_file():
        raise RuntimeDiscoveryError(
            f"runtime daemon metadata not found at {metadata}; start synapse-runtime "
            "with the same --state-dir or pass --runtime-port"
        )
    try:
        raw = metadata.read_bytes()
    except OSError as exc:  # pragma: no cover - read failure is environment specific
        raise RuntimeDiscoveryError(f"runtime daemon metadata could not be read: {exc}") from None
    if len(raw) > 4096:
        raise RuntimeDiscoveryError("runtime daemon metadata is unexpectedly large")
    try:
        value = json.loads(raw)
    except ValueError:
        raise RuntimeDiscoveryError("runtime daemon metadata is not valid JSON") from None
    host = value.get("host") if isinstance(value, dict) else None
    port = value.get("port") if isinstance(value, dict) else None
    if (
        type(host) is not str
        or host not in LOOPBACK_HOSTS
        or type(port) is not int
        or not 1 <= port <= 65535
    ):
        raise RuntimeDiscoveryError("runtime daemon metadata has no valid loopback host/port")
    return host, port