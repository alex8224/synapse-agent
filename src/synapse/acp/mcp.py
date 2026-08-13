"""ACP session MCP validation and conversion."""

from __future__ import annotations

from typing import Any

from synapse.integrations.mcp_client import McpServerConfig


class ACPMCPError(ValueError):
    """ACP MCP configuration is unsupported or unsafe."""


def _fields(value: Any, name: str) -> list[Any]:
    raw = getattr(value, name, None)
    return list(raw or []) if isinstance(raw, (list, tuple)) else []


def mcp_server_configs_from_acp(
    servers: list[Any] | tuple[Any, ...] | None,
) -> list[McpServerConfig]:
    """Convert ACP MCP server objects without persisting or logging credentials."""
    if not servers:
        return []
    result: list[McpServerConfig] = []
    names: set[str] = set()
    for server in servers:
        name = str(getattr(server, "name", "")).strip()
        if not name:
            raise ACPMCPError("mcp server name is required")
        if name in names:
            raise ACPMCPError(f"duplicate mcp server name: {name}")
        names.add(name)
        kind = str(getattr(server, "type", "stdio") or "stdio").casefold()
        if kind == "acp":
            raise ACPMCPError("ACP-backed MCP servers are not supported by this Agent")
        if kind == "http":
            headers = {
                str(item.name): str(item.value) for item in _fields(server, "headers")
            }
            result.append(
                McpServerConfig(
                    name=name,
                    transport="streamable_http",
                    url=str(getattr(server, "url", "") or ""),
                    headers=headers,
                )
            )
            continue
        if kind == "sse":
            headers = {
                str(item.name): str(item.value) for item in _fields(server, "headers")
            }
            result.append(
                McpServerConfig(
                    name=name,
                    transport="sse",
                    url=str(getattr(server, "url", "") or ""),
                    headers=headers,
                )
            )
            continue
        if kind not in {"stdio", ""}:
            raise ACPMCPError(f"unsupported mcp transport: {kind}")
        command = str(getattr(server, "command", "") or "").strip()
        if not command:
            raise ACPMCPError(f"stdio mcp server {name} requires command")
        env = {str(item.name): str(item.value) for item in _fields(server, "env")}
        args = [str(item) for item in list(getattr(server, "args", None) or [])]
        result.append(
            McpServerConfig(name=name, transport="stdio", command=command, args=args, env=env)
        )
    return result


def _effective_config(cfg: McpServerConfig) -> tuple[Any, ...]:
    """Comparable projection of the fields that define one MCP server."""
    return (
        cfg.transport,
        cfg.command,
        tuple(cfg.args),
        tuple(sorted(cfg.env.items())),
        cfg.url,
        tuple(sorted(cfg.headers.items())),
        bool(cfg.enabled),
        cfg.tool_prefix,
        tuple(cfg.include_tools or ()),
        tuple(cfg.exclude_tools or ()),
    )


def merge_mcp_server_configs(
    project: list[McpServerConfig] | None,
    client: list[McpServerConfig] | None,
) -> list[McpServerConfig]:
    """Merge layered project MCP config with Client MCP, rejecting conflicts.

    A same-named server with a different effective configuration is a hard
    conflict and fails closed; it is never silently overridden by either side.
    Different names are unioned, and identical duplicates collapse to one.
    """
    merged: dict[str, McpServerConfig] = {}
    for cfg in project or []:
        merged[cfg.name] = cfg
    for cfg in client or []:
        existing = merged.get(cfg.name)
        if existing is not None and _effective_config(existing) != _effective_config(cfg):
            raise ACPMCPError(f"mcp server name conflict: {cfg.name}")
        merged[cfg.name] = cfg
    return list(merged.values())
