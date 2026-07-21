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
      sessions.sqlite     # project layer typically
      checkpoints.sqlite
      history
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

SYNAPSE_DIRNAME = ".synapse"
MODELS_FILENAME = "models.json"
MCP_FILENAME = "mcp.json"
SETTINGS_FILENAME = "settings.json"
THEMES_FILENAME = "themes.json"


def user_config_dir() -> Path:
    return (Path.home() / SYNAPSE_DIRNAME).expanduser().resolve()


def project_config_dir(workspace: Path | str | None = None) -> Path:
    base = Path(workspace).expanduser().resolve() if workspace is not None else Path.cwd().resolve()
    return (base / SYNAPSE_DIRNAME).resolve()


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
