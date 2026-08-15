"""Persistence helpers for TUI-managed subagent model settings."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from synapse.settings.config_paths import SETTINGS_FILENAME, user_config_dir

_MODEL_KEY = "subagent_model_overrides"
_REASONING_KEY = "subagent_reasoning_effort_overrides"
_DEFAULT_MODEL_KEY = "subagent_default_model"
_DEFAULT_REASONING_KEY = "subagent_default_reasoning_effort"

# ``"inherit"`` means "skip this layer" (see resolve_subagent_model_config), so
# persisting it is equivalent to removing the value. Both default fields and
# override dicts are normalized the same way to keep files clean and the
# semantics consistent.
def _normalize_scalar(value: Any) -> str | None:
    value = value or None
    return None if value == "inherit" else value


def _normalize_overrides(value: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    for k, v in dict(value or {}).items():
        if v and v != "inherit":
            result[str(k)] = str(v)
    return result


def load_subagent_config(settings: Any) -> dict[str, Any]:
    """Return normalized effective values from Settings.

    ``"inherit"`` values are folded to ``None`` (both mean "this layer unset"),
    mirroring ``save_subagent_config`` so the TUI never sees raw ``"inherit"``
    from hand-edited files or environment variables.
    """
    return {
        "default_model": _normalize_scalar(
            getattr(settings, _DEFAULT_MODEL_KEY, None)
        ),
        "default_reasoning_effort": _normalize_scalar(
            getattr(settings, _DEFAULT_REASONING_KEY, None)
        ),
        "model_overrides": _normalize_overrides(
            getattr(settings, _MODEL_KEY, {})
        ),
        "reasoning_effort_overrides": _normalize_overrides(
            getattr(settings, _REASONING_KEY, {})
        ),
    }


def save_subagent_config(settings: Any, config: dict[str, Any]) -> Path:
    """Persist only subagent keys in the user-level settings JSON.

    The existing file is merged (other keys are preserved) and rewritten
    atomically via a temp file + ``os.replace``. A corrupt or non-object file
    raises ``ValueError`` instead of being silently overwritten so unrelated
    user settings cannot be lost.
    """
    path = user_config_dir() / SETTINGS_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"refusing to overwrite unreadable settings file: {path} ({exc})"
            ) from exc
        if not isinstance(loaded, dict):
            raise ValueError(f"settings file must contain a JSON object: {path}")
        existing = loaded

    values = {
        _DEFAULT_MODEL_KEY: _normalize_scalar(config.get("default_model")),
        _DEFAULT_REASONING_KEY: _normalize_scalar(
            config.get("default_reasoning_effort")
        ),
        _MODEL_KEY: _normalize_overrides(config.get("model_overrides")),
        _REASONING_KEY: _normalize_overrides(
            config.get("reasoning_effort_overrides")
        ),
    }
    for key, value in values.items():
        if value in (None, {}, []):
            existing.pop(key, None)
        else:
            existing[key] = value

    tmp_fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(existing, ensure_ascii=False, indent=2) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except OSError:
            # Cleanup failure must not shadow the os.replace() result.
            pass

    for key, value in values.items():
        fallback = {} if key.endswith("overrides") else None
        setattr(settings, key, value if value is not None else fallback)
    return path
