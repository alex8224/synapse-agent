"""Persistence for the speech engine the console lets a reader choose.

The choice has to do two things at once, and both are why this module mirrors
``subagent_config_persist`` rather than inventing a path:

- **survive a restart** -- it is written into the user settings layer
  (``~/.synapse/settings.json``) with the other non-secret preferences, merged so
  unrelated keys are preserved and rewritten atomically so a crash cannot leave a
  half-written file;
- **take effect immediately** -- the same call sets the value on the live
  ``Settings`` object, and the runtime reads ``manager.settings`` per request, so
  the very next ``runtime.stt.status`` already reports the new engine.  A daemon
  restart or a page reload is never required.

The model directory is stored as a string (JSON) and applied as a ``Path`` (the
field's type), so a hand-edited file and a console-chosen value end up identical.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from synapse.settings.config_paths import SETTINGS_FILENAME, user_config_dir
from synapse.stt.providers import provider_ids

__all__ = ["ENGINES", "load_stt_config", "save_stt_config"]

#: The engines a reader may choose between, taken from the provider registry
#: (``synapse.stt.providers``) rather than kept here.  A second, hand-kept copy is
#: what rejected ``doubao`` with a bare ``ValueError`` -- which the wire can only
#: render as "runtime service error" -- while the settings screen happily offered
#: it, because that screen is built from the registry.
ENGINES = provider_ids()

_ENGINE_KEY = "stt_engine"
_MODEL_DIR_KEY = "stt_model_dir"


def load_stt_config(settings: Any) -> dict[str, Any]:
    """The effective speech settings, normalized for a client to display.

    An unknown engine (a hand-edited file, or a value from a newer build) reads as
    ``browser``: the runtime would fall back to it anyway, and a UI that showed a
    mode nothing will run would be lying.
    """
    engine = getattr(settings, _ENGINE_KEY, None)
    if engine not in ENGINES:
        engine = "browser"
    model_dir = getattr(settings, _MODEL_DIR_KEY, None)
    return {"engine": engine, "model_dir": str(model_dir) if model_dir else None}


def save_stt_config(settings: Any, *, engine: str, model_dir: str | Path | None) -> Path:
    """Persist the choice and apply it to the live settings object.

    Args:
        settings: the running daemon's settings object; it is mutated in place.
        engine: ``browser`` or ``local``.
        model_dir: a model directory, or None to use the engine's own default.

    Returns:
        The settings file that was written.

    Raises:
        ValueError: the engine is unknown, or the existing settings file is
            unreadable or not a JSON object -- refusing to overwrite is what keeps
            unrelated user settings from being lost to a typo in a hand-edited file.
    """
    if engine not in ENGINES:
        raise ValueError(f"unknown speech engine: {engine!r}")
    directory = Path(str(model_dir)).expanduser() if model_dir else None

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

    # JSON carries the path as text; the live settings object carries it as a Path.
    for key, value in (
        (_ENGINE_KEY, engine),
        (_MODEL_DIR_KEY, str(directory) if directory else None),
    ):
        if value is None:
            existing.pop(key, None)
        else:
            existing[key] = value

    tmp_fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(existing, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except OSError:
            # Cleanup failure must not shadow the os.replace() result.
            pass

    # Applied last, so a failed write leaves the running daemon on its old engine
    # rather than on one that would not survive a restart.
    setattr(settings, _ENGINE_KEY, engine)
    setattr(settings, _MODEL_DIR_KEY, directory)
    return path
