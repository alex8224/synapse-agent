"""Query and result DTOs for the runtime configuration read surface.

This module owns the *read* DTOs only: the write ports live with the other
session commands (`RebindSessionCommand` for the model and
`SetThinkingLevelCommand` for the reasoning level, both session-scoped).
``RuntimeConfigView`` exposes only whitelisted display fields
(selected model, available model names, thinking levels, MCP server name /
transport / enabled / tool prefix).  It never carries API keys, environment
variables, headers, URLs, commands, arguments, goal state, or attachment
state, and it never serializes the underlying ``Settings`` object.
"""

from __future__ import annotations

from dataclasses import dataclass

from synapse.runtime.sessions.ref import SessionRef

__all__ = [
    "GetRuntimeConfigQuery",
    "MAX_RUNTIME_CONFIG_MCP_SERVERS",
    "MAX_RUNTIME_CONFIG_MODELS",
    "MAX_RUNTIME_CONFIG_TEXT_BYTES",
    "MAX_RUNTIME_CONFIG_THINKING_LEVELS",
    "McpServerView",
    "RuntimeConfigView",
]

#: Bounded model name list; a larger registry surfaces an explicit overflow
#: error instead of being silently truncated on the wire.
MAX_RUNTIME_CONFIG_MODELS = 256
#: Bounded thinking-level list (the shared catalog is small by construction).
MAX_RUNTIME_CONFIG_THINKING_LEVELS = 32
#: Bounded MCP server list exposed through the read-only view.
MAX_RUNTIME_CONFIG_MCP_SERVERS = 128
#: Byte bound for every text field projected into the view (names, transport,
#: tool prefixes, model aliases).  Never applies ``repr`` to a candidate value.
MAX_RUNTIME_CONFIG_TEXT_BYTES = 256


def _bounded_text(value: object, *, name: str) -> str:
    """Validate one non-empty whitelisted text value without echoing content."""
    if type(value) is not str or not value:
        raise ValueError(f"{name} must be a non-empty string")
    if "\x00" in value:
        raise ValueError(f"{name} must not contain NUL")
    try:
        size = len(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError:
        raise ValueError(f"{name} must be valid UTF-8") from None
    if size > MAX_RUNTIME_CONFIG_TEXT_BYTES:
        raise ValueError(f"{name} exceeds the length limit")
    return value


def _bounded_optional_text(value: object, *, name: str) -> str | None:
    if value is None:
        return None
    return _bounded_text(value, name=name)


def _bounded_names(value: object, *, name: str, maximum: int) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{name} must be a sequence")
    if len(value) > maximum:
        raise ValueError(f"{name} exceeds the size limit")
    if any(type(item) is not str for item in value):
        raise ValueError(f"{name} must contain only strings")
    return tuple(_bounded_text(item, name=name) for item in value)


@dataclass(frozen=True, slots=True)
class GetRuntimeConfigQuery:
    """Read the read-only runtime configuration for one session context.

    The session is used for authorization and for preferring session-bound
    settings over project settings when the session is already open.  Opening
    a session or building an agent is never a side effect of this query.
    """

    session: SessionRef

    def __post_init__(self) -> None:
        if type(self.session) is not SessionRef:
            raise ValueError("session must be a SessionRef")
        if not self.session.project_id or not self.session.thread_id:
            raise ValueError("session must have non-empty project_id and thread_id")


@dataclass(frozen=True, slots=True)
class McpServerView:
    """Whitelisted MCP server projection.

    Deliberately omits ``command``, ``args``, ``env``, ``url``, ``headers``,
    ``api_key``-style fields, and per-tool include/exclude lists.
    """

    name: str
    transport: str
    enabled: bool
    tool_prefix: str | None = None

    def __post_init__(self) -> None:
        _bounded_text(self.name, name="mcp server name")
        _bounded_text(self.transport, name="mcp server transport")
        if type(self.enabled) is not bool:
            raise ValueError("mcp server enabled must be a boolean")
        object.__setattr__(
            self,
            "tool_prefix",
            _bounded_optional_text(self.tool_prefix, name="mcp server tool_prefix"),
        )


@dataclass(frozen=True, slots=True)
class RuntimeConfigView:
    """Read-only projection of the effective runtime configuration.

    ``can_set_thinking`` is True when the session-scoped reasoning-level write
    port exists (it does: ``runtime.session.thinking.set``), and
    ``can_set_project_thinking`` does the same for the project-scoped default
    (``runtime.project.thinking.set``).
    ``project_thinking_level`` is the *project's own* default level, which is not
    necessarily the session's ``thinking_level``: a session may have rebound its
    reasoning level, and the project default only applies to sessions opened
    afterwards.  ``can_toggle_mcp_global`` is still always ``False`` — there is no
    global MCP write path — so clients render that one as read-only instead of
    pretending a save could succeed.
    """

    current_model: str
    available_models: tuple[str, ...]
    thinking_level: str | None
    thinking_levels: tuple[str, ...]
    mcp_servers: tuple[McpServerView, ...]
    mcp_enabled: bool
    can_set_thinking: bool = False
    can_toggle_mcp_global: bool = False
    project_thinking_level: str | None = None
    can_set_project_thinking: bool = False
    # The selected model's input context size, or None when its profile does not
    # declare one.  Clients need the denominator to render context occupancy
    # ("14.2k/200k"); the TUI reads the same number off the model profile.
    context_window: int | None = None
    # Whether the Codex usage / reset-credit entry is usable for this session.
    # True only when the composition root injected a usage provider, the session
    # is already open, and its *actual selected profile* uses Codex OAuth (never
    # inferred from the model name).  The console treats any other value as
    # "hidden, send no usage RPC", so the default is a hard False.
    codex_usage_enabled: bool = False

    def __post_init__(self) -> None:
        _bounded_text(self.current_model, name="current_model")
        object.__setattr__(
            self,
            "available_models",
            _bounded_names(
                self.available_models,
                name="available_models",
                maximum=MAX_RUNTIME_CONFIG_MODELS,
            ),
        )
        object.__setattr__(
            self,
            "thinking_level",
            _bounded_optional_text(self.thinking_level, name="thinking_level"),
        )
        object.__setattr__(
            self,
            "thinking_levels",
            _bounded_names(
                self.thinking_levels,
                name="thinking_levels",
                maximum=MAX_RUNTIME_CONFIG_THINKING_LEVELS,
            ),
        )
        object.__setattr__(
            self,
            "project_thinking_level",
            _bounded_optional_text(
                self.project_thinking_level, name="project_thinking_level"
            ),
        )
        servers = tuple(self.mcp_servers)
        if any(type(server) is not McpServerView for server in servers):
            raise ValueError("mcp_servers must contain McpServerView values")
        if len(servers) > MAX_RUNTIME_CONFIG_MCP_SERVERS:
            raise ValueError("mcp_servers exceeds the size limit")
        object.__setattr__(self, "mcp_servers", servers)
        for flag in (
            "mcp_enabled",
            "can_set_thinking",
            "can_toggle_mcp_global",
            "can_set_project_thinking",
            "codex_usage_enabled",
        ):
            if type(getattr(self, flag)) is not bool:
                raise ValueError(f"{flag} must be a boolean")
        window = self.context_window
        if window is not None and (type(window) is not int or window <= 0):
            raise ValueError("context_window must be a positive integer or None")
