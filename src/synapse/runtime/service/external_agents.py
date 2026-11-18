"""Runtime-owned catalog of local ACP executables (not project configuration)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

_MAX_CONFIG_BYTES = 64 * 1024
_AGENT_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}\Z")
_PROXY_KEYS = frozenset({"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"})


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


def _parse_agent(row: Any) -> LocalExternalAgent:
    allowed_keys = {"id", "name", "command", "args", "env", "permission_mode"}
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
    return LocalExternalAgent(
        ExternalAgentInfo(agent_id, name.strip(), permission_mode),
        command,
        tuple(args),
        MappingProxyType(env),
    )


class ExternalAgentCatalog:
    def __init__(self, agents: tuple[LocalExternalAgent, ...] = ()) -> None:
        self._agents = {agent.info.id: agent for agent in agents}
        if len(self._agents) != len(agents):
            raise ValueError("Duplicate external ACP agent id")

    @classmethod
    def from_state_dir(cls, state_dir: Path) -> ExternalAgentCatalog:
        """Read once at startup from the daemon's trusted state directory."""
        try:
            with (state_dir / "external-acp-agents.json").open("rb") as stream:
                raw = stream.read(_MAX_CONFIG_BYTES + 1)
        except FileNotFoundError:
            return cls()
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
        return cls(tuple(_parse_agent(row) for row in rows))

    def list(self) -> tuple[ExternalAgentInfo, ...]:
        return tuple(agent.info for agent in self._agents.values())

    def get(self, agent_id: str) -> LocalExternalAgent | None:
        return self._agents.get(agent_id)
