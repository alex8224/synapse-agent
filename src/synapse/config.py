"""Application settings for the coding agent."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from pydantic import Field, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from synapse.config_paths import (
    config_search_roots,
    executable_config_dirs,
    load_layered_settings_file,
    project_config_dir,
    user_config_dir,
)

# Re-export for callers/tests
__all__ = [
    "Settings",
    "bootstrap_project_env",
    "config_search_roots",
    "executable_config_dirs",
    "find_dotenv",
    "load_settings",
    "project_config_dir",
    "user_config_dir",
]


def find_dotenv(start: Path | None = None) -> Path | None:
    """Search for a `.env` file (legacy; prefer models.json api_key).

    Looks under workspace/cwd/exe/home upward for a few levels.
    """
    seen: set[Path] = set()
    for base in config_search_roots(start):
        cur = base
        for _ in range(6):
            if cur in seen:
                break
            seen.add(cur)
            env_path = cur / ".env"
            if env_path.is_file():
                return env_path
            if cur.parent == cur:
                break
            cur = cur.parent
    return None


def bootstrap_project_env(project_root: Path | None = None) -> Path | None:
    """Optionally load legacy `.env` (override process env).

    Preferred secret location is ``api_key`` inside layered ``models.json``.
    ``.env`` remains supported for migration/CI only.
    """
    env_path = find_dotenv(project_root)
    if env_path is None:
        return None
    load_dotenv(dotenv_path=env_path, override=True, encoding="utf-8")
    return env_path


class Settings(BaseSettings):
    """Runtime configuration from layered JSON + optional env / legacy `.env`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        env_ignore_empty=True,
    )

    # Model
    model: str = Field(default="openai:gpt-4.1", validation_alias="MODEL")
    openai_api_key: str | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    openai_base_url: str | None = Field(default=None, validation_alias="OPENAI_BASE_URL")
    anthropic_api_key: str | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    # Multi-model catalog (JSON file or inline JSON). Empty => legacy single model.
    models_config_path: Path | None = Field(default=None, validation_alias="AGENT_MODELS_CONFIG")
    models_json: str | None = Field(default=None, validation_alias="MODELS_JSON")
    # Selected profile alias (optional; falls back to model / registry default)
    active_model: str | None = Field(default=None, validation_alias="AGENT_ACTIVE_MODEL")

    # Workspace
    workspace: Path = Field(default_factory=Path.cwd, validation_alias="WORKSPACE")
    shell_timeout: int = Field(default=120, validation_alias="SHELL_TIMEOUT")
    max_output_bytes: int = Field(default=100_000, validation_alias="MAX_OUTPUT_BYTES")
    inherit_env: bool = Field(default=True, validation_alias="INHERIT_ENV")
    virtual_mode: bool = Field(default=True, validation_alias="VIRTUAL_MODE")
    # Shell executable. Default: pwsh (PowerShell 7+).
    # Values: pwsh | powershell | cmd | bash | system | absolute path
    shell_executable: str | None = Field(default="pwsh", validation_alias="SHELL_EXECUTABLE")
    # Decode shell stdout/stderr with this codec (avoids GBK UnicodeDecodeError on Windows).
    shell_encoding: str = Field(default="utf-8", validation_alias="SHELL_ENCODING")
    shell_encoding_errors: str = Field(
        default="replace", validation_alias="SHELL_ENCODING_ERRORS"
    )

    # Approval: default OFF, auto-pass (user requirement)
    require_approval: bool = Field(default=False, validation_alias="AGENT_REQUIRE_APPROVAL")
    auto_approve: bool = Field(default=True, validation_alias="AGENT_AUTO_APPROVE")
    safety_profile: str = Field(default="dev-autopass", validation_alias="AGENT_SAFETY_PROFILE")

    # Safety blacklist is advisory when auto_approve=True; still used for warnings
    enable_command_blacklist: bool = Field(
        default=True, validation_alias="ENABLE_COMMAND_BLACKLIST"
    )
    enable_compact_tool: bool = Field(
        default=True, validation_alias="AGENT_ENABLE_COMPACT_TOOL"
    )

    # Session / checkpoint
    checkpoint_backend: Literal["memory", "sqlite"] = Field(
        default="sqlite", validation_alias="CHECKPOINT_BACKEND"
    )
    checkpoint_path: Path = Field(
        default=Path(".coding-agent/checkpoints.sqlite"),
        validation_alias="CHECKPOINT_PATH",
    )
    sessions_path: Path | None = Field(default=None, validation_alias="SESSIONS_PATH")

    # Project memory / skills (paths relative to project root or absolute)
    memory_paths: list[str] = Field(
        default_factory=lambda: ["AGENTS.md", "MEMORY.md", ".coding-agent/MEMORY.md"]
    )
    skills_paths: list[str] = Field(default_factory=lambda: ["skills"])

    # Framework wiring (deepagents native)
    enable_subagents: bool = Field(default=True, validation_alias="AGENT_ENABLE_SUBAGENTS")
    subagent_tester_model: str | None = Field(
        default=None, validation_alias="AGENT_SUBAGENT_TESTER_MODEL"
    )
    subagent_reviewer_model: str | None = Field(
        default=None, validation_alias="AGENT_SUBAGENT_REVIEWER_MODEL"
    )
    readonly: bool = Field(default=False, validation_alias="AGENT_READONLY")
    excluded_tools: list[str] = Field(default_factory=list, validation_alias="AGENT_EXCLUDED_TOOLS")
    enable_fs_permissions: bool = Field(
        default=False, validation_alias="AGENT_ENABLE_FS_PERMISSIONS"
    )
    deny_fs_paths: list[str] = Field(default_factory=list, validation_alias="AGENT_DENY_FS_PATHS")

    # MCP extension (tools= injection)
    enable_mcp: bool = Field(default=True, validation_alias="AGENT_ENABLE_MCP")
    # When False (default), skip MCP connect during agent build; TUI attaches later.
    mcp_eager: bool = Field(default=False, validation_alias="AGENT_MCP_EAGER")
    # TUI: show UI first, build agent in a background thread.
    tui_defer_agent: bool = Field(default=True, validation_alias="AGENT_TUI_DEFER_AGENT")
    mcp_config_path: Path | None = Field(default=None, validation_alias="AGENT_MCP_CONFIG")

    mcp_servers_json: str | None = Field(default=None, validation_alias="MCP_SERVERS_JSON")

    # Observability
    langsmith_tracing: bool = Field(default=False, validation_alias="LANGSMITH_TRACING")
    langsmith_api_key: str | None = Field(default=None, validation_alias="LANGSMITH_API_KEY")
    langsmith_project: str = Field(default="coding-agent", validation_alias="LANGSMITH_PROJECT")

    # Debug
    debug: bool = Field(default=False, validation_alias="AGENT_DEBUG")

    # Streaming / concurrency
    token_stream: bool = Field(default=True, validation_alias="TOKEN_STREAM")
    parallel_tool_calls: bool = Field(default=True, validation_alias="PARALLEL_TOOL_CALLS")
    max_concurrency: int = Field(default=8, validation_alias="MAX_CONCURRENCY")
    # TUI / CLI appearance (see synapse.ui.theme).
    # Built-in dark: cursor-dark github-dark dracula nord solarized-dark
    #  catppuccin-mocha one-dark
    # Built-in light: solarized-light github-light one-light gruvbox-light
    #  catppuccin-latte tokyo-night-light ayu-light nord-light
    # Custom palettes: layered .coding-agent/themes.json
    theme: str = Field(default="cursor-dark", validation_alias="AGENT_THEME")

    # TUI tool timeline: keep tool-detail rows expanded under group headers.
    # Set false to auto-collapse batches after they finish (summary only).
    tool_details_expanded: bool = Field(
        default=True, validation_alias="AGENT_TOOL_DETAILS_EXPANDED"
    )
    # TUI session recap: after idle following a completed turn, show one-line summary.
    session_recap_enabled: bool = Field(
        default=True, validation_alias="AGENT_SESSION_RECAP"
    )
    session_recap_idle_seconds: float = Field(
        default=180.0, validation_alias="AGENT_SESSION_RECAP_IDLE_SECONDS"
    )
    session_recap_min_turns: int = Field(
        default=3, validation_alias="AGENT_SESSION_RECAP_MIN_TURNS"
    )
    # DeepSeek V4 thinking / other reasoning models via OpenAI-compatible API
    enable_thinking: bool = Field(default=True, validation_alias="ENABLE_THINKING")
    reasoning_effort: str = Field(default="high", validation_alias="REASONING_EFFORT")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Prefer project `.env` over process/user environment variables.

        Default pydantic-settings order lets a stale system OPENAI_API_KEY
        override the project-local key and cause 401 against private gateways.
        """
        return (
            init_settings,
            dotenv_settings,
            env_settings,
            file_secret_settings,
        )

    @field_validator("openai_api_key", "anthropic_api_key", "openai_base_url", mode="before")
    @classmethod
    def _strip_secret_like(cls, value: object) -> object:
        if isinstance(value, str):
            text = value.strip().strip("\"'")
            return text or None
        return value

    @field_validator("model", "active_model", "theme", mode="before")
    @classmethod
    def _strip_model(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().strip("\"'")
        return value

    @field_validator("workspace", "checkpoint_path", mode="before")
    @classmethod
    def _coerce_required_path(cls, value: object) -> Path:
        if value is None or value == "":
            return Path.cwd() if value is None else Path(value)
        return Path(value).expanduser().resolve()

    @field_validator(
        "sessions_path",
        "models_config_path",
        "mcp_config_path",
        mode="before",
    )
    @classmethod
    def _coerce_optional_path(cls, value: object) -> Path | None:
        if value is None or value == "":
            return None
        # Keep relative paths relative so load_settings can resolve against workspace.
        return Path(str(value)).expanduser()

    @field_validator("excluded_tools", "deny_fs_paths", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if value is None:
            return []
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    def ensure_dirs(self) -> None:
        """Create local state directories if needed."""
        self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        if self.sessions_path is not None:
            self.sessions_path.parent.mkdir(parents=True, exist_ok=True)

    def resolved_sessions_path(self) -> Path:
        if self.sessions_path is not None:
            return Path(self.sessions_path).expanduser().resolve()
        return self.checkpoint_path.parent / "sessions.sqlite"

    def resolved_memory_paths(self, project_root: Path | None = None) -> list[str]:
        root = project_root or Path.cwd()
        resolved: list[str] = []
        for p in self.memory_paths:
            path = Path(p) if Path(p).is_absolute() else (root / p)
            resolved.append(str(path.resolve()))
        return resolved

    def resolved_skills_paths(self, project_root: Path | None = None) -> list[str]:
        root = project_root or Path.cwd()
        paths: list[str] = []
        for p in self.skills_paths:
            path = Path(p) if Path(p).is_absolute() else (root / p)
            if path.exists():
                paths.append(str(path.resolve()))
        return paths

    def mask_openai_key(self) -> str:
        key = self.openai_api_key or ""
        if not key:
            return "<empty>"
        if len(key) <= 8:
            return "***"
        return f"len={len(key)} head={key[:4]}***tail=***{key[-4:]}"


def load_settings(**overrides: Any) -> Settings:
    """Load settings from layered config + optional env/legacy `.env`.

    Layers (later wins for settings.json / models / mcp):
      1. ``~/.coding-agent/``
      2. exe-adjacent ``.coding-agent/`` (portable bundle)
      3. ``<workspace>/.coding-agent/``

    Secrets: prefer ``api_key`` in ``models.json``. ``.env`` is legacy only.
    """
    project_root = overrides.get("workspace")
    root_path = Path(project_root).expanduser().resolve() if project_root is not None else None
    # Legacy: optional .env for migration
    bootstrap_project_env(root_path)

    env_path = find_dotenv(root_path)
    if env_path is not None:
        settings = Settings(_env_file=str(env_path))
    else:
        settings = Settings(_env_file=None)

    # Layered settings.json (user → project). Applied before CLI overrides.
    workspace_hint = root_path or settings.workspace
    file_cfg = load_layered_settings_file(workspace_hint)
    if file_cfg:
        allowed = set(Settings.model_fields.keys())
        updates: dict[str, Any] = {}
        for key, value in file_cfg.items():
            if key in allowed and value is not None:
                updates[key] = value
        if updates:
            # Path-like coercion for known path fields
            for pk in (
                "workspace",
                "checkpoint_path",
                "sessions_path",
                "models_config_path",
                "mcp_config_path",
            ):
                if pk in updates and updates[pk] is not None:
                    updates[pk] = Path(str(updates[pk])).expanduser()
            settings = settings.model_copy(update=updates)

    if overrides:
        # Non-path fields: ignore None (means "leave default / env").
        normal = {
            k: v
            for k, v in overrides.items()
            if v is not None
            and k
            not in {
                "models_config_path",
                "mcp_config_path",
                "sessions_path",
                "workspace",
                "checkpoint_path",
            }
        }
        if normal:
            settings = settings.model_copy(update=normal)
        # Optional paths may be explicitly cleared with None.
        pathish = {
            k: overrides[k]
            for k in ("models_config_path", "mcp_config_path", "sessions_path")
            if k in overrides
        }
        if pathish:
            coerced: dict[str, Any] = {}
            for k, v in pathish.items():
                if v is None or v == "":
                    coerced[k] = None
                else:
                    p = Path(v).expanduser()
                    coerced[k] = p.resolve() if p.is_absolute() else p
            settings = settings.model_copy(update=coerced)
        if "workspace" in overrides and overrides["workspace"] is not None:
            settings.workspace = Path(overrides["workspace"]).expanduser().resolve()
        if "checkpoint_path" in overrides and overrides["checkpoint_path"] is not None:
            settings.checkpoint_path = (
                Path(overrides["checkpoint_path"]).expanduser().resolve()
            )

    # Default state files live under project .coding-agent when possible.
    proj = project_config_dir(settings.workspace)
    path_updates: dict[str, Any] = {}
    try:
        default_ckpt = (Path.cwd() / ".coding-agent" / "checkpoints.sqlite").resolve()
    except Exception:  # noqa: BLE001
        default_ckpt = None
    if default_ckpt is not None and settings.checkpoint_path == default_ckpt:
        path_updates["checkpoint_path"] = proj / "checkpoints.sqlite"
    if settings.sessions_path is None:
        path_updates["sessions_path"] = proj / "sessions.sqlite"

    # Resolve relative models/mcp config paths against workspace (not process cwd).
    if settings.models_config_path is not None:
        mp = Path(settings.models_config_path).expanduser()
        if not mp.is_absolute():
            path_updates["models_config_path"] = (Path(settings.workspace) / mp).resolve()
    if settings.mcp_config_path is not None:
        cp = Path(settings.mcp_config_path).expanduser()
        if not cp.is_absolute():
            path_updates["mcp_config_path"] = (Path(settings.workspace) / cp).resolve()
    if path_updates:
        settings = settings.model_copy(update=path_updates)

    settings.ensure_dirs()

    # Layered models.json → selected profile (api_key / base_url / thinking).
    from synapse.models_registry import apply_models_config_to_settings

    settings = apply_models_config_to_settings(settings)

    # Activate UI theme (built-ins + layered themes.json). Soft-fail keeps boot ok.
    try:
        from synapse.ui.theme import bootstrap_theme

        bootstrap_theme(getattr(settings, "theme", None), workspace=settings.workspace)
    except Exception:  # noqa: BLE001
        pass
    return settings
