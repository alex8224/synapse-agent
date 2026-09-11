"""Whitelisted projection of effective runtime settings into config DTOs.

This module owns the *read-only* settings surface for
``LocalAgentRuntimeService.get_runtime_config``.  It reads a fixed set of
attributes off a settings-like object and resolves the model registry and MCP
server configs, then projects them into :class:`RuntimeConfigView` fields.
Only whitelisted display data is returned:

- model names (registry aliases) and the selected model alias/name,
- thinking levels for the current model plus the effective reasoning level,
- MCP server ``name`` / ``transport`` / ``enabled`` / ``tool_prefix``,
- the global MCP enable flag and the (always False) capability flags.

Secrets (API keys, ``env``, ``headers``, ``url``, ``command``/``args``,
provider base URLs, workspace-absolute paths from Settings, goals, attachment
state) are never read here and can never reach the view.  Core read errors from
the registry or the MCP config loader are *not* swallowed: they propagate to
the caller unchanged.  Only the bounded view construction maps to an explicit
``ConfigOverflowError`` so an oversized registry/config surfaces a typed error
instead of silent truncation.
"""

from __future__ import annotations

from typing import Any

from synapse.integrations.mcp_client import load_mcp_server_configs
from synapse.models.config import DEFAULT_THINKING_LEVELS
from synapse.models.registry import registry_from_settings
from synapse.runtime.service.errors import ConfigOverflowError
from synapse.runtime.service.runtime_config import (
    MAX_RUNTIME_CONFIG_MCP_SERVERS,
    MAX_RUNTIME_CONFIG_MODELS,
    MAX_RUNTIME_CONFIG_TEXT_BYTES,
    MAX_RUNTIME_CONFIG_THINKING_LEVELS,
    McpServerView,
    RuntimeConfigView,
)
from synapse.runtime.sessions.ref import SessionRef

__all__ = ["build_config_view"]


def _attr(settings: Any, name: str, default: Any = None) -> Any:
    """Read one whitelisted settings attribute; never iterates the object."""
    return getattr(settings, name, default)


def _model_text(value: Any) -> str | None:
    """Coerce a whitelisted model alias/name to a bounded string or None."""
    if value is None:
        return None
    if type(value) is not str:
        raise ConfigOverflowError("runtime model setting is invalid")
    text = value.strip()
    if not text:
        return None
    try:
        size = len(text.encode("utf-8", errors="strict"))
    except UnicodeEncodeError:
        raise ConfigOverflowError("runtime model setting is invalid") from None
    if size > MAX_RUNTIME_CONFIG_TEXT_BYTES:
        raise ConfigOverflowError("runtime model setting exceeds the length limit")
    return text


def build_config_view(settings: Any, *, session: SessionRef) -> RuntimeConfigView:
    """Project one effective settings object into a read-only config view.

    ``settings`` is the session-bound settings when the target session is
    already open, or the project settings otherwise (the caller decides).
    ``session`` is only used to name the config context; no secret is derived
    from it.
    """
    del session  # display context only; values come from whitelisted settings
    registry = registry_from_settings(settings)

    current = (
        _model_text(_attr(settings, "active_model"))
        or _model_text(getattr(registry, "default", None) if registry is not None else None)
        or _model_text(_attr(settings, "model"))
    )
    if current is None:
        raise ConfigOverflowError("no model is selected for this session")

    if registry is None:
        raise ConfigOverflowError("model registry is unavailable")

    names = registry.list_names()
    if len(names) > MAX_RUNTIME_CONFIG_MODELS:
        raise ConfigOverflowError("model registry exceeds the model count limit")

    available = tuple(_model_text(name) for name in names)
    if any(name is None for name in available):
        raise ConfigOverflowError("model registry contains an invalid model name")

    try:
        allowed = list(registry.allowed_thinking_levels(current))
    except Exception:  # noqa: BLE001 - registry quirks degrade to the shared list
        allowed = list(getattr(registry, "thinking_levels", None) or ())
    if not allowed:
        allowed = list(DEFAULT_THINKING_LEVELS)
    if len(allowed) > MAX_RUNTIME_CONFIG_THINKING_LEVELS:
        raise ConfigOverflowError("thinking levels exceed the level count limit")
    thinking_levels = tuple(_model_text(level) for level in allowed)
    if any(level is None for level in thinking_levels):
        raise ConfigOverflowError("thinking levels contain an invalid level")

    enable_thinking = bool(_attr(settings, "enable_thinking", True))
    effort = _model_text(_attr(settings, "reasoning_effort"))
    thinking_level = None
    if enable_thinking and effort is not None and effort in thinking_levels:
        thinking_level = effort

    mcp_enabled = bool(_attr(settings, "enable_mcp", True))
    server_views: list[McpServerView] = []
    if mcp_enabled:
        servers = load_mcp_server_configs(
            path=_attr(settings, "mcp_config_path"),
            json_blob=_attr(settings, "mcp_servers_json"),
            workspace=_attr(settings, "workspace"),
        )
        if len(servers) > MAX_RUNTIME_CONFIG_MCP_SERVERS:
            raise ConfigOverflowError("MCP server config exceeds the server count limit")
        for server in servers:
            name = _model_text(getattr(server, "name", None))
            transport = _model_text(getattr(server, "transport", None))
            if name is None or transport is None:
                raise ConfigOverflowError("MCP server config is missing its name or transport")
            tool_prefix = _model_text(getattr(server, "tool_prefix", None))
            enabled = getattr(server, "enabled", True)
            if type(enabled) is not bool:
                raise ConfigOverflowError("MCP server enabled flag is invalid")
            try:
                server_views.append(
                    McpServerView(
                        name=name,
                        transport=transport,
                        enabled=enabled,
                        tool_prefix=tool_prefix,
                    )
                )
            except ValueError as exc:
                raise ConfigOverflowError("MCP server config exceeds the safety bound") from exc

    try:
        return RuntimeConfigView(
            current_model=current,
            available_models=available,
            thinking_level=thinking_level,
            thinking_levels=thinking_levels,
            mcp_servers=tuple(server_views),
            mcp_enabled=mcp_enabled,
            can_set_thinking=False,
            can_toggle_mcp_global=False,
        )
    except ValueError as exc:
        raise ConfigOverflowError("runtime config exceeds the safety bound") from exc
