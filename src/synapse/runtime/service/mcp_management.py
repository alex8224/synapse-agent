"""Request/result DTOs for the MCP server CRUD and maintenance surface.

Pure data only: this module contains no implementation or network code, only
validated wire types for listing, creating, updating, and deleting configured
MCP servers.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from synapse.runtime.sessions.ref import SessionRef

__all__ = [
    "DeleteMcpServerCommand",
    "ListMcpServersQuery",
    "MCP_SERVER_ALLOWED_KEYS",
    "McpServerDetailView",
    "McpServerListResult",
    "SaveMcpServerCommand",
]

MCP_SERVER_ALLOWED_KEYS = frozenset(
    {
        "name",
        "transport",
        "command",
        "args",
        "env",
        "url",
        "headers",
        "enabled",
        "tool_prefix",
        "include_tools",
        "exclude_tools",
        "timeout",
        "proxy",
    }
)

MCP_SERVER_NAME_MAX_LENGTH = 128
_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]+$")


def _validate_server_name(value: object, field_name: str = "name") -> str:
    if type(value) is not str:
        raise ValueError(f"{field_name} must be a string")
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field_name} must not be empty")
    if len(cleaned) > MCP_SERVER_NAME_MAX_LENGTH:
        raise ValueError(f"{field_name} must be at most {MCP_SERVER_NAME_MAX_LENGTH} characters")
    for char in cleaned:
        if char.isspace() or unicodedata.category(char).startswith("C"):
            raise ValueError(f"{field_name} must not contain control or whitespace characters")
    return cleaned


def _validate_server_payload(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("server must be a mapping")
    unknown = set(value) - MCP_SERVER_ALLOWED_KEYS
    if unknown:
        raise ValueError(f"server has unsupported keys: {sorted(unknown)}")
    for key in value:
        if type(key) is not str:
            raise ValueError("server keys must be strings")
    return value


@dataclass(frozen=True, slots=True)
class ListMcpServersQuery:
    """List all configured MCP servers and their live state for the session."""

    session: SessionRef


@dataclass(frozen=True, slots=True)
class McpServerDetailView:
    """Detailed projection of one MCP server configuration and live state."""

    name: str
    transport: str
    enabled: bool
    command: str | None = None
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    url: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    tool_prefix: str | None = None
    include_tools: tuple[str, ...] = ()
    exclude_tools: tuple[str, ...] = ()
    timeout: float | None = None
    proxy: str | None = None
    attached: bool = False
    discovered: tuple[str, ...] = ()
    loaded: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class McpServerListResult:
    """All configured MCP servers for the project and global MCP status."""

    servers: tuple[McpServerDetailView, ...]
    mcp_enabled: bool
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SaveMcpServerCommand:
    """Add or update one MCP server in the project/user configuration."""

    session: SessionRef
    server: Mapping[str, Any]
    original_name: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "server", _validate_server_payload(self.server))
        if self.original_name is not None:
            _validate_server_name(self.original_name, "original_name")


@dataclass(frozen=True, slots=True)
class DeleteMcpServerCommand:
    """Remove one MCP server by name from configuration."""

    session: SessionRef
    name: str

    def __post_init__(self) -> None:
        _validate_server_name(self.name, "name")
