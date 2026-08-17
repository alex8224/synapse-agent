"""CRUD persistence for layered ``models.json`` files.

All writes go through one atomic replace (temp file + ``os.replace``). The
user-facing store is the project-local layer (``<workspace>/.synapse/models.json``)
unless ``settings.models_config_path`` pins an explicit file. Keys already
present at the top level (``headers``, ``thinking_levels``, ``vision_model``, ...)
are preserved on rewrite.

Invariants enforced by :func:`validate_models_data`:
  * root is a JSON object
  * ``models`` is a non-empty object of profile dicts
  * ``default`` references an existing profile (or the first profile when unset)

Deleting the last remaining profile is refused so the file never becomes
unloadable.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from synapse.models.profile import ModelProfile
from synapse.models.registry import _profiles_from_mapping, resolve_models_config_paths
from synapse.settings.config_paths import MODELS_FILENAME, project_config_dir

# Alias appears in /model reload commands; keep it a single safe token.
_VALID_ALIAS = re.compile(r"^[A-Za-z0-9_.:-]+$")


class ModelsStoreError(ValueError):
    """A models.json operation is invalid or the file cannot be safely written."""


# ModelProfile dataclass fields that are serially stored next to the profile key.
_PROFILE_FIELDS = (
    "model",
    "api_key",
    "api_key_env",
    "auth",
    "base_url",
    "headers",
    "context_window",
    "enable_thinking",
    "reasoning_effort",
    "thinking_levels",
    "parallel_tool_calls",
    "websocket",
    "image_input",
    "extra",
    "model_kwargs",
    "extra_body",
)


def models_store_path(settings: Any) -> Path:
    """Target file for CRUD writes.

    An explicit ``settings.models_config_path`` wins; otherwise the project
    layer ``<workspace>/.synapse/models.json`` is used so changes are shared
    with collaborators without touching the user-global store.
    """
    explicit = getattr(settings, "models_config_path", None)
    if explicit:
        workspace = getattr(settings, "workspace", None) or Path.cwd()
        p = Path(explicit).expanduser()
        if not p.is_absolute():
            p = (Path(workspace).expanduser().resolve() / p).resolve()
        else:
            p = p.resolve()
        return p
    workspace = getattr(settings, "workspace", None) or Path.cwd()
    return (project_config_dir(workspace) / MODELS_FILENAME).resolve()


def load_models_store(settings: Any) -> dict[str, Any]:
    """Read the CRUD target file, or return an empty (unvalidated) scaffold.

    A missing file is treated as ``{"models": {}}``; the caller decides whether
    adding the first profile is allowed. A corrupt or non-object file raises
    :class:`ModelsStoreError` instead of being silently overwritten.
    """
    path = models_store_path(settings)
    if not path.is_file():
        return {"models": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ModelsStoreError(f"cannot read models config: {path} ({exc})") from exc
    if not isinstance(raw, dict):
        raise ModelsStoreError(f"models config must be a JSON object: {path}")
    return raw


def profile_to_dict(profile: ModelProfile) -> dict[str, Any]:
    """Serialize one profile, dropping empty values for a clean file."""
    out: dict[str, Any] = {}
    for field in _PROFILE_FIELDS:
        value = getattr(profile, field)
        if value in (None, "", {}, []):
            continue
        if field in {"thinking_levels"} and isinstance(value, tuple):
            value = list(value)
        out[field] = value
    return out


def validate_models_data(data: dict[str, Any]) -> None:
    """Run the same validation registry parsing uses; raise ModelsStoreError."""
    if not isinstance(data.get("models"), dict) or not data["models"]:
        raise ModelsStoreError("models config must contain a non-empty 'models' object")
    try:
        _profiles_from_mapping(data)
    except ValueError as exc:
        raise ModelsStoreError(str(exc)) from exc


def save_models_store(settings: Any, data: dict[str, Any]) -> Path:
    """Validate and atomically write ``data``; refresh ``models_config_path``.

    The settings object gets ``models_config_path`` repointed at the written
    file with ``model_copy`` when it is a pydantic model, or via direct
    attribute assignment otherwise, so subsequent registry loads see the new
    store without a restart.
    """
    validate_models_data(data)
    path = models_store_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)

    tmp_fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        if _contains_plaintext_api_key(data):
            try:
                os.chmod(tmp_name, 0o600)
            except OSError:
                pass
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except OSError:
            # Cleanup failure must not shadow the os.replace() result.
            pass

    _refresh_settings_path(settings, path)
    return path


def _contains_plaintext_api_key(data: dict[str, Any]) -> bool:
    for profile in (data.get("models") or {}).values():
        if isinstance(profile, dict) and profile.get("api_key"):
            return True
    return False


def _refresh_settings_path(settings: Any, path: Path) -> None:
    """Point ``models_config_path`` at the written file (pydantic-safe)."""
    attr_name = "models_config_path"
    try:
        if hasattr(settings, "model_copy") and callable(settings.model_copy):
            # Replace in place only when the object supports it; pydantic is frozen.
            try:
                settings.__dict__[attr_name] = path
                return
            except Exception:  # noqa: BLE001
                pass
        setattr(settings, attr_name, path)
    except Exception:  # noqa: BLE001
        # Settings writeability varies across call sites; best-effort only.
        pass


def current_default(data: dict[str, Any]) -> str | None:
    """Effective default profile name, or the first profile when unset."""
    models = data.get("models") or {}
    default = data.get("default")
    if default in models:
        return str(default)
    return next(iter(models), None)


def add_profile(settings: Any, name: str, profile: ModelProfile | dict[str, Any]) -> Path:
    """Add (or fully replace) one profile and persist. ``name`` == alias."""
    name = str(name).strip()
    if not name:
        raise ModelsStoreError("model alias must not be empty")
    if not _VALID_ALIAS.match(name):
        raise ModelsStoreError(
            f"model alias {name!r} may only contain letters, digits, '.', '_', ':' or '-'"
        )
    data = load_models_store(settings)
    models = data.setdefault("models", {})
    if not isinstance(models, dict):
        raise ModelsStoreError("models config 'models' must be an object")
    if name in models:
        raise ModelsStoreError(f"model alias already exists: {name}")
    if isinstance(profile, ModelProfile):
        models[name] = profile_to_dict(profile)
    elif isinstance(profile, dict):
        models[name] = _normalize_profile_dict(dict(profile), name)
    else:
        raise ModelsStoreError("profile must be a ModelProfile or dict")
    if len(models) == 1:
        # Only the very first profile seeds an implicit default; adding to an
        # existing store must not silently change its effective default.
        data["default"] = name
    return save_models_store(settings, data)


def _normalize_profile_dict(payload: dict[str, Any], name: str) -> dict[str, Any]:
    """Keep known profile fields and validate the required ``model`` value."""
    if not str(payload.get("model") or "").strip():
        raise ModelsStoreError(f"model profile {name!r} requires 'model'")
    out: dict[str, Any] = {}
    for field in _PROFILE_FIELDS:
        if field not in payload:
            continue
        value = payload[field]
        if value in (None, "", {}, []):
            continue
        if field == "thinking_levels" and isinstance(value, tuple):
            value = list(value)
        out[field] = value
    return out


def update_profile(settings: Any, name: str, changes: dict[str, Any]) -> Path:
    """Merge ``changes`` into one profile (``None`` value deletes the key)."""
    data = load_models_store(settings)
    models = data.get("models") or {}
    if name not in models:
        raise ModelsStoreError(f"unknown model alias: {name}")
    current = dict(models[name])
    for key, value in changes.items():
        if value in (None, ""):
            if key == "model":
                raise ModelsStoreError(f"model profile {name} requires {key!r}")
            current.pop(key, None)
        else:
            current[key] = value
    models[name] = current
    return save_models_store(settings, data)


def delete_profile(settings: Any, name: str) -> Path:
    """Remove one profile; refuse when it is the last remaining one."""
    data = load_models_store(settings)
    models = data.get("models") or {}
    if name not in models:
        raise ModelsStoreError(f"unknown model alias: {name}")
    if len(models) <= 1:
        raise ModelsStoreError("cannot delete the last model profile")
    del models[name]
    if data.get("default") == name:
        data["default"] = next(iter(models))
    return save_models_store(settings, data)


def set_default(settings: Any, name: str) -> Path:
    """Persist ``name`` as the default model profile."""
    data = load_models_store(settings)
    models = data.get("models") or {}
    if name not in models:
        raise ModelsStoreError(f"unknown model alias: {name}")
    data["default"] = name
    return save_models_store(settings, data)


def resolve_write_targets(settings: Any) -> list[Path]:
    """Files that would participate in a fresh registry load (for previews)."""
    return resolve_models_config_paths(settings)