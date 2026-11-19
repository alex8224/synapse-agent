"""Runtime-owned catalog of local ACP executables (not project configuration)."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

_LOGGER = logging.getLogger(__name__)

_MAX_CONFIG_BYTES = 64 * 1024
_AGENT_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}\Z")
_PROXY_KEYS = frozenset({"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"})
# `session/new` is the *peer's* own work, not a Synapse call: an agent that checks
# its cloud account and model catalogue first legitimately needs tens of seconds
# (measured: 5-30s for `agy`, minutes when its cloud path is unreachable). These
# bounds only stop a typo from parking a session open for hours.
_MIN_CREATE_TIMEOUT_S = 5.0
_MAX_CREATE_TIMEOUT_S = 600.0


@dataclass(frozen=True, slots=True)
class ExternalAgentInfo:
    id: str
    name: str
    permission_mode: Literal["manual", "full_access"] = "manual"


@dataclass(frozen=True, slots=True)
class LocalExternalAgent:
    info: ExternalAgentInfo
    command: str = field(repr=False)
    args: tuple[str, ...] = field(repr=False)
    proxy_env: MappingProxyType[str, str] = field(repr=False)
    #: Per-agent budget for the peer's `session/new`; `None` keeps the default.
    create_timeout_s: float | None = None


def _parse_agent(row: Any) -> LocalExternalAgent:
    allowed_keys = {
        "id",
        "name",
        "command",
        "args",
        "env",
        "permission_mode",
        "create_timeout_s",
    }
    if not isinstance(row, dict) or set(row) - allowed_keys:
        raise ValueError("Invalid external ACP agent entry")
    permission_mode = row.get("permission_mode", "manual")
    if permission_mode not in ("manual", "full_access"):
        raise ValueError("Invalid external ACP permission mode")
    agent_id, name, command = (row.get(key) for key in ("id", "name", "command"))
    if not isinstance(agent_id, str) or not _AGENT_ID.fullmatch(agent_id):
        raise ValueError("Invalid external ACP agent id")
    if not isinstance(name, str) or not name.strip() or len(name) > 128:
        raise ValueError("Invalid external ACP agent name")
    if not isinstance(command, str) or not command.strip() or not Path(command).is_absolute():
        raise ValueError("External ACP agent command must be an absolute executable path")
    if "\0" in command or Path(command).suffix.lower() in {".bat", ".cmd"}:
        raise ValueError("External ACP agent command must not be a Windows batch file")
    args = row.get("args", [])
    if not isinstance(args, list) or not all(
        isinstance(arg, str) and "\0" not in arg for arg in args
    ):
        raise ValueError("Invalid external ACP agent arguments")
    if permission_mode != "full_access" and any(
        arg.split("=", 1)[0] in {"--dangerously-skip-permissions", "-dangerously-skip-permissions"}
        for arg in args
    ):
        raise ValueError("The ACP skip-permissions flag requires full_access mode")
    # Agent-specific flags are passed through unchanged; full_access is a display
    # declaration and does not make Synapse approve unexpected ACP callbacks.
    env = row.get("env", {})
    if not isinstance(env, dict) or any(
        key not in _PROXY_KEYS or not isinstance(value, str) or "\0" in value
        for key, value in env.items()
    ):
        raise ValueError("Only supported proxy environment overrides are allowed")
    create_timeout_s = row.get("create_timeout_s")
    if create_timeout_s is not None:
        # `bool` subclasses `int`, so a JSON `true` must be rejected explicitly.
        if isinstance(create_timeout_s, bool) or not isinstance(create_timeout_s, (int, float)):
            raise ValueError("Invalid external ACP create timeout")
        if not _MIN_CREATE_TIMEOUT_S <= create_timeout_s <= _MAX_CREATE_TIMEOUT_S:
            raise ValueError("External ACP create timeout is out of range")
        create_timeout_s = float(create_timeout_s)
    return LocalExternalAgent(
        ExternalAgentInfo(agent_id, name.strip(), permission_mode),
        command,
        tuple(args),
        MappingProxyType(env),
        create_timeout_s,
    )


class ExternalAgentCatalog:
    def __init__(
        self,
        agents: tuple[LocalExternalAgent, ...] = (),
        *,
        config_path: Path | None = None,
        stat_key: tuple[int, int] | None = None,
    ) -> None:
        self._agents = {agent.info.id: agent for agent in agents}
        if len(self._agents) != len(agents):
            raise ValueError("Duplicate external ACP agent id")
        self._config_path = config_path
        self._stat_key = stat_key

    @staticmethod
    def _load_file(
        config_path: Path,
    ) -> tuple[tuple[LocalExternalAgent, ...], tuple[int, int] | None]:
        try:
            st = config_path.stat()
            with config_path.open("rb") as stream:
                raw = stream.read(_MAX_CONFIG_BYTES + 1)
        except FileNotFoundError:
            return (), None
        if len(raw) > _MAX_CONFIG_BYTES:
            raise ValueError("External ACP configuration exceeds the size limit")
        try:
            data = json.loads(raw)
        except (UnicodeError, ValueError, RecursionError):
            raise ValueError("Invalid external ACP configuration JSON") from None
        if not isinstance(data, dict) or set(data) != {"agents"}:
            raise ValueError("External ACP configuration must contain an agents list")
        rows = data["agents"]
        if not isinstance(rows, list) or len(rows) > 32:
            raise ValueError("External ACP configuration must contain at most 32 agents")
        parsed = tuple(_parse_agent(row) for row in rows)
        if len({agent.info.id for agent in parsed}) != len(parsed):
            raise ValueError("Duplicate external ACP agent id")
        return parsed, (st.st_mtime_ns, st.st_size)

    @classmethod
    def from_state_dir(cls, state_dir: Path) -> ExternalAgentCatalog:
        """Read from the daemon's trusted state directory and hot-reload on file change."""
        config_path = state_dir / "external-acp-agents.json"
        agents, stat_key = cls._load_file(config_path)
        return cls(agents, config_path=config_path, stat_key=stat_key)

    def _maybe_reload(self) -> None:
        if self._config_path is None:
            return
        try:
            st = self._config_path.stat()
            current_key: tuple[int, int] | None = (st.st_mtime_ns, st.st_size)
        except FileNotFoundError:
            if self._stat_key is not None:
                self._agents = {}
                self._stat_key = None
            return
        except OSError:
            return
        if current_key == self._stat_key:
            return
        try:
            agents, stat_key = self._load_file(self._config_path)
        except (OSError, ValueError):
            # Degradation boundary: keep last known good catalog while user edits file.
            _LOGGER.warning(
                "Failed to hot-reload external ACP configuration; keeping previous catalog"
            )
            return
        self._agents = {agent.info.id: agent for agent in agents}
        self._stat_key = stat_key

    def list(self) -> tuple[ExternalAgentInfo, ...]:
        self._maybe_reload()
        return tuple(agent.info for agent in self._agents.values())

    def get(self, agent_id: str) -> LocalExternalAgent | None:
        self._maybe_reload()
        return self._agents.get(agent_id)
