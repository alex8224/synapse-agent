"""Model configuration parsing and normalization helpers."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")

DEFAULT_MODELS_CONFIG_REL = Path(".coding-agent") / "models.json"

THINKING_LEVELS = ("minimal", "low", "medium", "high", "max")
DEFAULT_THINKING_LEVELS: tuple[str, ...] = ("off", *THINKING_LEVELS)

_PROFILE_META_KEYS = {
    "model", "api_key", "api_key_env", "base_url", "websocket",
    "context_window", "contextwindow", "max_input_tokens", "enable_thinking",
    "thinking", "thinking_level", "reasoning_effort", "thinking_levels",
    "parallel_tool_calls", "image_input", "capabilities", "extra",
    "model_kwargs", "extra_body", "params",
}

def expand_env_string(value: Any) -> Any:
    """Expand ${VAR} / $VAR in string config values."""
    if not isinstance(value, str):
        return value

    def repl(match: re.Match[str]) -> str:
        key = match.group(1) or match.group(2)
        return os.environ.get(key, "")

    return _ENV_PATTERN.sub(repl, value)


def parse_context_window(cfg: dict[str, Any] | None) -> int | None:
    """Read context window (tokens) from a models.json profile object.

    Accepts ``context_window``, ``contextwindow``, or ``max_input_tokens``.
    Returns a positive int, or None when unset/invalid.
    """
    if not isinstance(cfg, dict):
        return None
    raw = cfg.get("context_window")
    if raw is None:
        raw = cfg.get("contextwindow")
    if raw is None:
        raw = cfg.get("max_input_tokens")
    if raw is None or raw is False:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return value


def apply_context_window_to_model(model: Any, context_window: int | None) -> Any:
    """Stamp ``max_input_tokens`` onto a chat model profile for summarization."""
    if model is None or context_window is None:
        return model
    try:
        size = int(context_window)
    except (TypeError, ValueError):
        return model
    if size <= 0:
        return model
    existing = getattr(model, "profile", None)
    profile: dict[str, Any]
    if isinstance(existing, dict):
        profile = dict(existing)
    else:
        profile = {}
    profile["max_input_tokens"] = size
    try:
        model.profile = profile
    except Exception:  # noqa: BLE001
        # Some wrappers expose read-only profile; best-effort only.
        try:
            object.__setattr__(model, "profile", profile)
        except Exception:  # noqa: BLE001
            pass
    return model


def parse_optional_bool(value: Any, *, field_name: str = "image_input") -> bool | None:
    """Parse JSON and common string boolean values without truthiness surprises."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "1", "yes", "on", "enabled"}:
            return True
        if normalized in {"false", "0", "no", "off", "disabled"}:
            return False
    raise ValueError(f"{field_name} must be a boolean")


def normalize_thinking_level(value: Any) -> str | None:
    """Normalize thinking level / reasoning_effort to a canonical string.

    Accepts: off|minimal|low|medium|high|max, plus common aliases
    (min, med, xhigh, ultra).
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return "high" if value else "off"
    text = str(value).strip().casefold()
    if not text:
        return None
    if text in {"off", "false", "0", "disabled", "none", "no"}:
        return "off"
    aliases = {
        "min": "minimal",
        "minimal": "minimal",
        "low": "low",
        "medium": "medium",
        "med": "medium",
        "mid": "medium",
        "high": "high",
        "max": "max",
        "maximum": "max",
        "xhigh": "max",
        "ultra": "max",
        "highest": "max",
    }
    if text in aliases:
        return aliases[text]
    # pass through unknown provider-specific values (e.g. "xhigh" already mapped)
    return str(value).strip()


def parse_thinking_levels(raw: Any) -> list[str] | None:
    """Parse a thinking_levels array into canonical labels.

    Returns None when unset. Accepts strings like off|low|medium|high|max.
    """
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise ValueError("thinking_levels must be an array of strings")
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        level = normalize_thinking_level(item)
        if not level:
            raise ValueError(f"invalid thinking level in thinking_levels: {item!r}")
        if level in seen:
            continue
        seen.add(level)
        out.append(level)
    if not out:
        raise ValueError("thinking_levels must not be empty")
    return out


def default_thinking_levels() -> list[str]:
    """Built-in allowed thinking levels."""
    return list(DEFAULT_THINKING_LEVELS)


def parse_thinking_config(cfg: dict[str, Any]) -> tuple[bool | None, str | None]:
    """Return (enable_thinking, reasoning_effort) from profile JSON.

    Priority:
      1) thinking: false | \"off\" | \"disabled\" => disabled
      2) thinking / thinking_level / reasoning_effort string => level
      3) enable_thinking bool
    """
    enable: bool | None = None
    level: str | None = None

    raw_thinking = cfg.get("thinking", None)
    if raw_thinking is not None:
        if isinstance(raw_thinking, bool):
            enable = raw_thinking
            if raw_thinking:
                level = normalize_thinking_level(
                    cfg.get("thinking_level")
                    or cfg.get("reasoning_effort")
                    or "high"
                )
            else:
                level = None
        else:
            text = str(raw_thinking).strip().casefold()
            if text in {"off", "false", "0", "disabled", "none", "no"}:
                enable = False
                level = None
            elif text in {"on", "true", "1", "enabled", "yes"}:
                enable = True
                level = normalize_thinking_level(
                    cfg.get("thinking_level") or cfg.get("reasoning_effort") or "high"
                )
            else:
                enable = True
                level = normalize_thinking_level(raw_thinking)

    if "thinking_level" in cfg and cfg.get("thinking_level") is not None:
        enable = True if enable is None else enable
        if enable is not False:
            level = normalize_thinking_level(cfg.get("thinking_level")) or level

    if "reasoning_effort" in cfg and cfg.get("reasoning_effort") is not None:
        enable = True if enable is None else enable
        if enable is not False:
            level = normalize_thinking_level(cfg.get("reasoning_effort")) or level

    if "enable_thinking" in cfg and cfg.get("enable_thinking") is not None:
        enable = bool(cfg.get("enable_thinking"))
        if enable and level is None:
            level = normalize_thinking_level(
                cfg.get("thinking_level")
                or cfg.get("reasoning_effort")
                or "high"
            )
        if not enable:
            level = None

    return enable, level


def _coerce_params(cfg: dict[str, Any]) -> dict[str, Any]:
    """Collect custom ChatModel / request parameters from profile config."""
    params: dict[str, Any] = {}
    for key in ("params", "extra"):
        raw = cfg.get(key)
        if isinstance(raw, dict):
            params.update(raw)
    # Common top-level ChatModel kwargs
    for key, value in cfg.items():
        if key in _PROFILE_META_KEYS:
            continue
        params[key] = value
    return params
