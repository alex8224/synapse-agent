"""Layered synapse configuration roots.

Two primary layers (later overrides earlier on merge):

1. User global: ``~/.synapse/``
2. Project local: ``<workspace>/.synapse/``

Optional portable layer: directory next to a frozen / non-python exe.

File layout (either layer)::

    .synapse/
      models.json         # model profiles + api_key (preferred over .env)
      mcp.json            # MCP servers
      settings.json       # non-secret Settings overrides (includes theme)
      themes.json         # optional custom UI themes (merged user → project)
      system_prompt.md    # coding agent system prompt (user/project override)
      agents/             # subagent definitions (*.md, merged user → project)
      sessions.sqlite     # project layer typically
      checkpoints.sqlite
      history
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

SYNAPSE_DIRNAME = ".synapse"
MODELS_FILENAME = "models.json"
MCP_FILENAME = "mcp.json"
SETTINGS_FILENAME = "settings.json"
THEMES_FILENAME = "themes.json"
AGENTS_DIRNAME = "agents"


def user_config_dir() -> Path:
    return (Path.home() / SYNAPSE_DIRNAME).expanduser().resolve()


def project_config_dir(workspace: Path | str | None = None) -> Path:
    base = Path(workspace).expanduser().resolve() if workspace is not None else Path.cwd().resolve()
    return (base / SYNAPSE_DIRNAME).resolve()


def user_agents_dir() -> Path:
    """User-global subagent definitions directory (``~/.synapse/agents/``)."""
    return user_config_dir() / AGENTS_DIRNAME


def project_agents_dir(workspace: Path | str | None = None) -> Path:
    """Project-local subagent definitions directory (``<workspace>/.synapse/agents/``)."""
    return project_config_dir(workspace) / AGENTS_DIRNAME


def layered_agents_dirs(
    workspace: Path | str | None = None,
    *,
    include_exe: bool = True,
) -> list[Path]:
    """Ordered subagent definition dirs: user → (exe) → project.

    Merge rule mirrors ``layered_config_dirs``: later entries override earlier
    ones for the same subagent ``name``.
    """
    dirs: list[Path] = [user_agents_dir()]
    if include_exe:
        for d in executable_config_dirs():
            if d.name == SYNAPSE_DIRNAME:
                dirs.append(d / AGENTS_DIRNAME)
            else:
                dirs.append(d / SYNAPSE_DIRNAME / AGENTS_DIRNAME)
    dirs.append(project_agents_dir(workspace))
    seen: set[Path] = set()
    ordered: list[Path] = []
    for d in dirs:
        try:
            key = d.resolve()
        except Exception:  # noqa: BLE001
            key = d
        if key in seen:
            continue
        seen.add(key)
        ordered.append(key)
    return ordered


def executable_config_dirs() -> list[Path]:
    """Dirs next to the running binary (frozen exe / non-python entrypoint)."""
    out: list[Path] = []
    try:
        if getattr(sys, "frozen", False):
            out.append(Path(sys.executable).resolve().parent)
            meipass = getattr(sys, "_MEIPASS", None)
            if meipass:
                out.append(Path(meipass).resolve())
            return out
        exe = Path(sys.executable).resolve()
        if exe.suffix.lower() == ".exe" and not exe.stem.lower().startswith("python"):
            out.append(exe.parent)
        if sys.argv:
            argv0 = Path(sys.argv[0]).resolve()
            if argv0.suffix.lower() == ".exe":
                out.append(argv0.parent)
    except Exception:  # noqa: BLE001
        return out
    return out


def layered_config_dirs(
    workspace: Path | str | None = None,
    *,
    include_exe: bool = True,
) -> list[Path]:
    """Ordered config dirs: user → (exe) → project.

    Merge rule: later entries override earlier ones for the same keys/profiles.
    """
    dirs: list[Path] = [user_config_dir()]
    if include_exe:
        for d in executable_config_dirs():
            # Treat portable bundle as a layer between user and project.
            dirs.append(d / SYNAPSE_DIRNAME if d.name != SYNAPSE_DIRNAME else d)
    dirs.append(project_config_dir(workspace))
    seen: set[Path] = set()
    ordered: list[Path] = []
    for d in dirs:
        try:
            key = d.resolve()
        except Exception:  # noqa: BLE001
            key = d
        if key in seen:
            continue
        seen.add(key)
        ordered.append(key)
    return ordered


def config_search_roots(start: Path | None = None) -> list[Path]:
    """Roots used for legacy `.env` discovery (workspace/cwd/exe upward)."""
    roots: list[Path] = []
    if start is not None:
        try:
            roots.append(Path(start).expanduser().resolve())
        except Exception:  # noqa: BLE001
            roots.append(Path(start))
    try:
        roots.append(Path.cwd().resolve())
    except Exception:  # noqa: BLE001
        roots.append(Path.cwd())
    roots.extend(executable_config_dirs())
    roots.append(Path.home())
    seen: set[Path] = set()
    ordered: list[Path] = []
    for r in roots:
        try:
            key = r.resolve()
        except Exception:  # noqa: BLE001
            key = r
        if key in seen:
            continue
        seen.add(key)
        ordered.append(key)
    return ordered


def existing_files(dirs: Iterable[Path], filename: str) -> list[Path]:
    out: list[Path] = []
    for d in dirs:
        p = Path(d) / filename
        try:
            if p.is_file():
                out.append(p.resolve())
        except OSError:
            continue
    return out


def load_json_object(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} root must be a JSON object")
    return data


def deep_merge_dict(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursive dict merge; override wins. Lists are replaced, not concatenated."""
    out = dict(base)
    for key, value in override.items():
        if (
            key in out
            and isinstance(out[key], dict)
            and isinstance(value, dict)
        ):
            out[key] = deep_merge_dict(out[key], value)
        else:
            out[key] = value
    return out


def load_layered_json(
    filename: str,
    workspace: Path | str | None = None,
) -> tuple[dict[str, Any] | None, list[Path]]:
    """Load and merge JSON objects from layered dirs. Returns (merged, paths_used)."""
    paths = existing_files(layered_config_dirs(workspace), filename)
    if not paths:
        return None, []
    merged: dict[str, Any] = {}
    for path in paths:
        merged = deep_merge_dict(merged, load_json_object(path))
    return merged, paths


def load_layered_settings_file(workspace: Path | str | None = None) -> dict[str, Any]:
    data, _ = load_layered_json(SETTINGS_FILENAME, workspace)
    return dict(data or {})


def models_config_paths(workspace: Path | str | None = None) -> list[Path]:
    return existing_files(layered_config_dirs(workspace), MODELS_FILENAME)


def mcp_config_paths(workspace: Path | str | None = None) -> list[Path]:
    return existing_files(layered_config_dirs(workspace), MCP_FILENAME)


def set_mcp_server_enabled(
    server_name: str,
    enabled: bool,
    *,
    workspace: Path | str | None = None,
    explicit_path: Path | str | None = None,
) -> Path:
    """Update one server flag in its highest-priority MCP config file."""
    if not server_name.strip():
        raise ValueError("server_name must not be empty")
    paths = [Path(explicit_path).expanduser().resolve()] if explicit_path else mcp_config_paths(
        workspace
    )
    if not paths:
        raise FileNotFoundError("no MCP config file is available")
    path = paths[-1]
    data = load_json_object(path)
    servers = data.get("servers")
    if not isinstance(servers, list):
        raise ValueError("MCP config servers must be a list")
    for server in servers:
        if isinstance(server, dict) and server.get("name") == server_name:
            server["enabled"] = enabled
            break
    else:
        raise KeyError(server_name)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def read_project_thinking_default(workspace: Path | str | None = None) -> str | None:
    """Read the project layer's *explicit* reasoning default, or ``None``.

    Only ``<workspace>/.synapse/settings.json`` is consulted: this is the layer
    :func:`set_project_reasoning_effort` writes, so the value reported here is
    exactly the project's own default rather than an inherited one.

    The effective ``reasoning_effort`` of a loaded ``Settings`` object cannot be
    used for this: ``apply_models_config_to_settings`` re-seeds
    ``reasoning_effort`` / ``enable_thinking`` from the selected model profile on
    every load, so a settings-layer default would be invisible after a reload
    (which is why the runtime applies this value to newly opened sessions
    explicitly).  A missing file, a missing key and a malformed file all report
    ``None`` instead of raising: a stale default must never break a read.
    """
    path = project_config_dir(workspace) / SETTINGS_FILENAME
    if not path.is_file():
        return None
    try:
        data = load_json_object(path)
    except (OSError, ValueError):
        return None
    if data.get("enable_thinking") is False:
        return "off"
    effort = data.get("reasoning_effort")
    if type(effort) is str and effort.strip():
        return effort.strip()
    return None


def set_project_reasoning_effort(
    level: str,
    *,
    workspace: Path | str | None = None,
) -> Path:
    """Persist one project's default reasoning level into its settings layer.

    The target is ``<workspace>/.synapse/settings.json`` — the layer
    :func:`load_project_settings` merges last for that workspace — and it is
    created (with its directory) on first use, because a project that never had a
    settings file must still be able to record a default.

    ``off`` is stored as ``enable_thinking: false`` while the previous
    ``reasoning_effort`` is preserved, so re-enabling thinking restores the level
    the project had before.  Every other level stores ``enable_thinking: true``
    plus the level itself.  Level validation stays with the caller (the daemon
    applies the token through ``apply_thinking_to_settings`` against the live
    whitelist first), so this remains a pure persistence helper.

    The write is atomic (temp file + ``replace``), so an interrupted write can
    never leave a half-written settings file behind.
    """
    if type(level) is not str or not level.strip():
        raise ValueError("level must not be empty")
    directory = project_config_dir(workspace)
    path = directory / SETTINGS_FILENAME
    data: dict[str, Any] = load_json_object(path) if path.is_file() else {}
    token = level.strip()
    if token == "off":
        data["enable_thinking"] = False
    else:
        data["enable_thinking"] = True
        data["reasoning_effort"] = token
    directory.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    temporary.replace(path)
    return path
