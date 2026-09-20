"""Provider credentials, stored server-side and never echoed back.

A cloud engine needs a key, and a key is the one thing the console must be able to
*write* without ever being able to *read*.  So it lives in its own user-level file
(``~/.synapse/stt.json``), beside the other per-concern config files, and this
module is the only reader:

- the console sends a key through ``runtime.stt.set_api_key`` and learns back only
  whether one is configured (``key_configured``), never the value;
- the write is atomic and merges, so a second provider's key cannot erase the
  first one's;
- an unreadable or non-object file is refused rather than overwritten, because
  losing a key the reader pasted once is worse than a failed save.

The file is deliberately separate from ``settings.json``: that file is documented as
non-secret overrides, and a key must not end up in a layer that is safe to commit,
share or print.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from synapse.settings.config_paths import STT_FILENAME, user_config_dir

__all__ = ["api_key", "configured", "save_api_key", "stt_config_path"]

#: Longest accepted key.  A bound exists so a paste accident cannot write a
#: megabyte into a config file the daemon reads on every status call.
MAX_API_KEY_CHARS = 512


def stt_config_path() -> Path:
    """The user-level speech config file (secret-bearing)."""
    return user_config_dir() / STT_FILENAME


def _read() -> dict[str, Any]:
    path = stt_config_path()
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"refusing to use unreadable speech config: {path} ({exc})") from exc
    if not isinstance(loaded, dict):
        raise ValueError(f"speech config must contain a JSON object: {path}")
    return loaded


def api_key(provider_id: str) -> str | None:
    """The stored key for one provider, or None.

    Callers inside the daemon use this to open a connection; nothing on the wire
    may return it (see ``synapse.runtime.service.stt``).
    """
    try:
        document = _read()
    except ValueError:
        # An unreadable file means "no key configured" from the caller's point of
        # view: the console then reports the provider as unusable with a reason,
        # instead of failing every status read until someone edits the file by hand.
        return None
    providers = document.get("providers")
    if not isinstance(providers, dict):
        return None
    entry = providers.get(provider_id)
    if not isinstance(entry, dict):
        return None
    value = entry.get("api_key")
    return value if isinstance(value, str) and value else None


def configured(provider_id: str) -> bool:
    """Whether a usable key is stored, without reading it into a caller."""
    return api_key(provider_id) is not None


def save_api_key(provider_id: str, key: str) -> Path:
    """Store one provider's key, merging with whatever is already there.

    Args:
        provider_id: the provider the key belongs to.
        key: the key; an empty value removes the stored one.

    Returns:
        The file that was written.

    Raises:
        ValueError: the key is malformed, or the existing file cannot be read.
    """
    cleaned = key.strip()
    if cleaned and (len(cleaned) > MAX_API_KEY_CHARS or "\x00" in cleaned):
        raise ValueError("speech api key is invalid")
    if not provider_id or "/" in provider_id or "\\" in provider_id:
        raise ValueError("speech provider id is invalid")

    document = _read()
    providers = document.get("providers")
    if not isinstance(providers, dict):
        providers = {}
    entry = providers.get(provider_id)
    if not isinstance(entry, dict):
        entry = {}
    if cleaned:
        entry["api_key"] = cleaned
    else:
        entry.pop("api_key", None)
    if entry:
        providers[provider_id] = entry
    else:
        providers.pop(provider_id, None)
    if providers:
        document["providers"] = providers
    else:
        document.pop("providers", None)

    path = stt_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(document, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except OSError:
            # Cleanup failure must not shadow the os.replace() result.
            pass
    return path
