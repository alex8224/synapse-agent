"""Multi-model registry built on top of deepagents/LangChain model assembly.

Preferred configuration: `.coding-agent/models.json`
Secrets stay in env / `.env` via `api_key_env`.

Top-level fields:
  default, models,
  thinking_levels (array): allowed session thinking levels, shared by models
  default_thinking: optional global default when a profile omits thinking

Profile fields:
  model, api_key_env, base_url,
  context_window / contextwindow / max_input_tokens (int): model context size in
    tokens. Wired into LangChain ``model.profile["max_input_tokens"]`` so
    deepagents summarization can use fraction-based compact triggers
    (default ~85% / keep ~10%). When omitted, compact falls back to fixed
    ~170k-token thresholds.
  thinking_levels (optional array): subset of top-level levels for this model
  thinking / thinking_level / reasoning_effort / enable_thinking: profile default only
  temperature, max_tokens, timeout, top_p, ... (ChatModel kwargs),
  model_kwargs (request body kwargs),
  extra_body (provider-specific body merge),
  extra (legacy free-form kwargs for init_chat_model)

Runtime thinking is session-scoped (Settings / /model thinking). Profile values
only seed defaults when a model is selected; they do not lock the effort level.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langchain.chat_models import init_chat_model

from synapse.llm_openai_compat import (
    deepseek_thinking_kwargs,
    enable_openai_compat_reasoning_patch,
)

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")

DEFAULT_MODELS_CONFIG_REL = Path(".coding-agent") / "models.json"

# Thinking / reasoning effort aliases accepted in models.json
THINKING_LEVELS = ("minimal", "low", "medium", "high", "max")
DEFAULT_THINKING_LEVELS: tuple[str, ...] = ("off", *THINKING_LEVELS)

# Keys reserved for profile metadata (not forwarded as ChatModel kwargs)
_PROFILE_META_KEYS = {
    "model",
    "api_key",
    "api_key_env",
    "base_url",
    "context_window",
    "contextwindow",
    "max_input_tokens",
    "enable_thinking",
    "thinking",
    "thinking_level",
    "reasoning_effort",
    "thinking_levels",
    "parallel_tool_calls",
    "extra",
    "model_kwargs",
    "extra_body",
    "params",
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


@dataclass(frozen=True)
class ModelProfile:
    """One named model endpoint.

    Thinking fields here are **defaults only**. Session thinking lives on Settings
    and is applied at build time via explicit enable_thinking/reasoning_effort.
    """

    name: str
    model: str
    api_key: str | None = None
    api_key_env: str | None = None
    base_url: str | None = None
    # Model input context size (tokens). Used by compact/summarization thresholds.
    context_window: int | None = None
    enable_thinking: bool | None = None
    reasoning_effort: str | None = None
    # Optional allowed levels for this model (subset of registry.thinking_levels)
    thinking_levels: tuple[str, ...] | None = None
    parallel_tool_calls: bool | None = None
    # Free-form kwargs for init_chat_model (temperature, max_tokens, timeout, ...)
    extra: dict[str, Any] = field(default_factory=dict)
    # Request body kwargs merged into model_kwargs
    model_kwargs: dict[str, Any] = field(default_factory=dict)
    # Provider-specific body (merged into extra_body)
    extra_body: dict[str, Any] = field(default_factory=dict)

    def resolved_api_key(self) -> str | None:
        if self.api_key:
            return expand_env_string(self.api_key) or None
        if self.api_key_env:
            env_name = str(expand_env_string(self.api_key_env) or "")
            return os.environ.get(env_name) or None
        return None

    def thinking_label(self) -> str:
        if self.enable_thinking is False:
            return "off"
        if self.reasoning_effort:
            return str(self.reasoning_effort)
        if self.enable_thinking is True:
            return "on"
        return "default"


@dataclass
class ModelRegistry:
    """Alias catalog for chat models + shared thinking level catalog."""

    profiles: dict[str, ModelProfile]
    default: str
    # Allowed thinking levels for the session (/model thinking ...).
    thinking_levels: list[str] = field(default_factory=default_thinking_levels)
    # Optional global default when a profile omits thinking config.
    default_thinking: str | None = None

    def list_names(self) -> list[str]:
        return sorted(self.profiles)

    def get(self, name: str | None = None) -> ModelProfile:
        key = (name or self.default or "").strip()
        if not key:
            raise KeyError("no model profile selected")
        if key in self.profiles:
            return self.profiles[key]
        # Match concrete provider:model ids stored on profiles.
        for prof in self.profiles.values():
            if prof.model == key:
                return prof
        short = key.split(":", 1)[1] if ":" in key else key
        for prof in self.profiles.values():
            if short_model_id(prof.model) == short:
                return prof
        if ":" in key:
            # Ad-hoc provider:model with no credentials from models.json
            return ModelProfile(name=key, model=key)
        raise KeyError(f"unknown model profile: {key}")

    def allowed_thinking_levels(self, name: str | None = None) -> list[str]:
        """Effective thinking levels for a model (registry ∩ optional profile subset)."""
        base = list(self.thinking_levels or default_thinking_levels())
        try:
            profile = self.get(name)
        except KeyError:
            return base
        if not profile.thinking_levels:
            return base
        allowed = set(profile.thinking_levels)
        filtered = [level for level in base if level in allowed]
        return filtered or list(profile.thinking_levels)

    def build_chat_model(
        self,
        name: str | None = None,
        *,
        fallback_api_key: str | None = None,
        fallback_base_url: str | None = None,
        fallback_enable_thinking: bool = True,
        fallback_reasoning_effort: str = "high",
        fallback_parallel_tool_calls: bool = True,
        fallback_stream_chunk_timeout: float | None = None,
        enable_thinking: bool | None = None,
        reasoning_effort: str | None = None,
    ):
        """Construct a LangChain chat model for the selected profile.

        Thinking resolution order:
          1. Explicit ``enable_thinking`` / ``reasoning_effort`` (session Settings)
          2. Profile defaults
          3. ``fallback_*`` kwargs

        For OpenAI-compatible models, ``stream_chunk_timeout`` defaults to the
        session fallback (usually disabled) so long reasoning pauses do not
        raise langchain-openai's StreamChunkTimeoutError. Profile ``params`` /
        top-level ``stream_chunk_timeout`` still win when present.
        """
        from synapse.startup_trace import span

        with span("model:openai_compat_patch"):
            enable_openai_compat_reasoning_patch()
        profile = self.get(name)
        kwargs: dict[str, Any] = dict(profile.extra or {})
        model_name = profile.model

        api_key = profile.resolved_api_key() or fallback_api_key
        base_url = profile.base_url or fallback_base_url

        if enable_thinking is None:
            resolved_enable = (
                fallback_enable_thinking
                if profile.enable_thinking is None
                else profile.enable_thinking
            )
        else:
            resolved_enable = bool(enable_thinking)

        if reasoning_effort is None:
            resolved_effort = profile.reasoning_effort or fallback_reasoning_effort
        else:
            resolved_effort = reasoning_effort

        if not resolved_enable:
            resolved_effort = None

        parallel = (
            fallback_parallel_tool_calls
            if profile.parallel_tool_calls is None
            else profile.parallel_tool_calls
        )

        if model_name.startswith("openai:"):
            if base_url:
                kwargs["base_url"] = str(base_url).rstrip("/")
            if api_key:
                kwargs["api_key"] = api_key
            kwargs.setdefault("use_responses_api", False)
            kwargs.setdefault("streaming", True)
            # Override langchain-openai default 120s silence killer unless profile set it.
            if "stream_chunk_timeout" not in kwargs:
                kwargs["stream_chunk_timeout"] = fallback_stream_chunk_timeout

            model_kwargs = dict(kwargs.get("model_kwargs") or {})
            model_kwargs.update(dict(profile.model_kwargs or {}))
            if parallel:
                model_kwargs.setdefault("parallel_tool_calls", True)
            kwargs["model_kwargs"] = model_kwargs

            # Thinking / reasoning level + optional user extra_body merge
            think_kwargs = deepseek_thinking_kwargs(
                enabled=bool(resolved_enable),
                reasoning_effort=str(resolved_effort or "high"),
            )
            extra_body = dict(think_kwargs.get("extra_body") or {})
            user_body = dict(profile.extra_body or {})
            if user_body:
                # Deep merge one level for thinking key if both present
                if "thinking" in extra_body and isinstance(user_body.get("thinking"), dict):
                    merged_thinking = dict(extra_body["thinking"])
                    merged_thinking.update(user_body["thinking"])
                    user_body = dict(user_body)
                    user_body["thinking"] = merged_thinking
                extra_body.update(user_body)
            if resolved_enable:
                kwargs["reasoning_effort"] = think_kwargs.get(
                    "reasoning_effort", resolved_effort
                )
            # Always set extra_body so disable path works
            existing_body = dict(kwargs.get("extra_body") or {})
            existing_body.update(extra_body)
            kwargs["extra_body"] = existing_body
            # Long keep-alive for SDK-default httpx pools (no shared client injection).
            from synapse.http_clients import enable_long_keepalive_http_defaults

            enable_long_keepalive_http_defaults()
        elif model_name.startswith("anthropic:"):
            if api_key:
                kwargs["api_key"] = api_key
                kwargs["anthropic_api_key"] = api_key
            if base_url:
                # ChatAnthropic accepts base_url alias for anthropic_api_url
                kwargs["base_url"] = str(base_url).rstrip("/")
            kwargs.setdefault("streaming", True)
            if profile.model_kwargs:
                mk = dict(kwargs.get("model_kwargs") or {})
                mk.update(profile.model_kwargs)
                kwargs["model_kwargs"] = mk
            # ChatAnthropic has a first-class `thinking` field; map from extra_body.
            body = dict(profile.extra_body or {})
            thinking_cfg = body.pop("thinking", None)
            if thinking_cfg is not None and "thinking" not in kwargs:
                kwargs["thinking"] = thinking_cfg
            if body:
                mk = dict(kwargs.get("model_kwargs") or {})
                mk.update(body)
                kwargs["model_kwargs"] = mk

        with span("model:init_chat_model"):
            chat_model = init_chat_model(model_name, **kwargs)
        return apply_context_window_to_model(chat_model, profile.context_window)


def _profiles_from_mapping(data: dict[str, Any]) -> ModelRegistry:
    raw_models = data.get("models") or {}
    if not isinstance(raw_models, dict) or not raw_models:
        raise ValueError("models config must contain a non-empty 'models' object")

    top_levels = parse_thinking_levels(data.get("thinking_levels"))
    thinking_levels = top_levels or default_thinking_levels()
    default_thinking = normalize_thinking_level(data.get("default_thinking"))

    profiles: dict[str, ModelProfile] = {}
    for name, cfg in raw_models.items():
        if not isinstance(cfg, dict):
            raise ValueError(f"model profile {name!r} must be an object")
        model = str(expand_env_string(cfg.get("model") or "")).strip()
        if not model:
            raise ValueError(f"model profile {name!r} missing 'model'")

        enable_thinking, reasoning_effort = parse_thinking_config(cfg)
        # Profile omits thinking -> optional top-level default_thinking.
        if enable_thinking is None and reasoning_effort is None and default_thinking:
            if default_thinking == "off":
                enable_thinking, reasoning_effort = False, None
            else:
                enable_thinking, reasoning_effort = True, default_thinking

        profile_levels = parse_thinking_levels(cfg.get("thinking_levels"))
        if profile_levels is not None:
            catalog = set(thinking_levels)
            profile_levels = [
                level for level in profile_levels if level in catalog
            ] or profile_levels

        params = _coerce_params(cfg)
        # Expand env in string params
        expanded_params: dict[str, Any] = {}
        for k, v in params.items():
            expanded_params[k] = expand_env_string(v) if isinstance(v, str) else v

        base_url = cfg.get("base_url")
        if base_url is not None:
            base_url = str(expand_env_string(base_url)).strip() or None

        model_kwargs = cfg.get("model_kwargs") or {}
        if not isinstance(model_kwargs, dict):
            raise ValueError(f"model profile {name!r} model_kwargs must be an object")
        extra_body = cfg.get("extra_body") or {}
        if not isinstance(extra_body, dict):
            raise ValueError(f"model profile {name!r} extra_body must be an object")

        # model_kwargs / extra_body may also appear inside params — peel them out
        if "model_kwargs" in expanded_params and isinstance(expanded_params["model_kwargs"], dict):
            merged_mk = dict(model_kwargs)
            merged_mk.update(expanded_params.pop("model_kwargs"))
            model_kwargs = merged_mk
        if "extra_body" in expanded_params and isinstance(expanded_params["extra_body"], dict):
            merged_eb = dict(extra_body)
            merged_eb.update(expanded_params.pop("extra_body"))
            extra_body = merged_eb

        parallel = cfg.get("parallel_tool_calls")
        if parallel is None and "parallel_tool_calls" in expanded_params:
            parallel = expanded_params.pop("parallel_tool_calls")

        context_window = parse_context_window(cfg)
        # Peel accidental copies from free-form params (meta keys should already
        # exclude these; keep defensive cleanup for nested params/extra).
        for key in ("context_window", "contextwindow", "max_input_tokens"):
            expanded_params.pop(key, None)

        profiles[str(name)] = ModelProfile(
            name=str(name),
            model=model,
            api_key=cfg.get("api_key"),
            api_key_env=cfg.get("api_key_env"),
            base_url=base_url,
            context_window=context_window,
            enable_thinking=enable_thinking,
            reasoning_effort=reasoning_effort,
            thinking_levels=tuple(profile_levels) if profile_levels else None,
            parallel_tool_calls=None if parallel is None else bool(parallel),
            extra=expanded_params,
            model_kwargs=dict(model_kwargs),
            extra_body=dict(extra_body),
        )
    default = str(expand_env_string(data.get("default") or next(iter(profiles))))
    if default not in profiles:
        raise ValueError(f"default model {default!r} not in models")
    return ModelRegistry(
        profiles=profiles,
        default=default,
        thinking_levels=thinking_levels,
        default_thinking=default_thinking,
    )


def load_models_config(path: Path | str | None) -> ModelRegistry | None:
    """Load models JSON if path exists; return None when unset/missing."""
    if path is None:
        return None
    p = Path(path).expanduser()
    if not p.is_file():
        return None
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("models config root must be an object")
    return _profiles_from_mapping(data)


def load_models_json_blob(blob: str | None) -> ModelRegistry | None:
    if not blob or not blob.strip():
        return None
    data = json.loads(blob)
    if not isinstance(data, dict):
        raise ValueError("MODELS_JSON root must be an object")
    return _profiles_from_mapping(data)


def merge_model_profiles(base: ModelProfile, override: ModelProfile) -> ModelProfile:
    """Merge two profiles with the same name; override wins non-empty fields."""
    return ModelProfile(
        name=override.name or base.name,
        model=override.model or base.model,
        api_key=override.api_key if override.api_key not in (None, "") else base.api_key,
        api_key_env=(
            override.api_key_env
            if override.api_key_env not in (None, "")
            else base.api_key_env
        ),
        base_url=override.base_url if override.base_url not in (None, "") else base.base_url,
        context_window=(
            override.context_window
            if override.context_window is not None
            else base.context_window
        ),
        enable_thinking=(
            override.enable_thinking
            if override.enable_thinking is not None
            else base.enable_thinking
        ),
        reasoning_effort=(
            override.reasoning_effort
            if override.reasoning_effort not in (None, "")
            else base.reasoning_effort
        ),
        thinking_levels=(
            override.thinking_levels
            if override.thinking_levels is not None
            else base.thinking_levels
        ),
        parallel_tool_calls=(
            override.parallel_tool_calls
            if override.parallel_tool_calls is not None
            else base.parallel_tool_calls
        ),
        extra={**base.extra, **override.extra},
        model_kwargs={**base.model_kwargs, **override.model_kwargs},
        extra_body={**base.extra_body, **override.extra_body},
    )


def merge_model_registries(
    base: ModelRegistry | None,
    override: ModelRegistry | None,
) -> ModelRegistry | None:
    if base is None:
        return override
    if override is None:
        return base
    profiles = dict(base.profiles)
    for name, prof in override.profiles.items():
        if name in profiles:
            profiles[name] = merge_model_profiles(profiles[name], prof)
        else:
            profiles[name] = prof
    default = override.default if override.default in profiles else base.default
    if default not in profiles:
        default = next(iter(profiles))
    # Prefer non-default thinking_levels from override layer when customized.
    if override.thinking_levels and override.thinking_levels != list(DEFAULT_THINKING_LEVELS):
        thinking_levels = list(override.thinking_levels)
    elif base.thinking_levels:
        thinking_levels = list(base.thinking_levels)
    else:
        thinking_levels = default_thinking_levels()
    default_thinking = (
        override.default_thinking
        if override.default_thinking not in (None, "")
        else base.default_thinking
    )
    return ModelRegistry(
        profiles=profiles,
        default=default,
        thinking_levels=thinking_levels,
        default_thinking=default_thinking,
    )


def default_models_config_path(workspace: Path | str | None = None) -> Path:
    """Canonical project path: <workspace>/.coding-agent/models.json."""
    base = Path(workspace).expanduser().resolve() if workspace is not None else Path.cwd()
    return (base / DEFAULT_MODELS_CONFIG_REL).resolve()


def resolve_models_config_paths(settings: Any) -> list[Path]:
    """All models.json files that participate in the merge (user → project).

    If ``settings.models_config_path`` is set, only that explicit file is used.
    """
    from synapse.config_paths import models_config_paths

    explicit = getattr(settings, "models_config_path", None)
    workspace = getattr(settings, "workspace", None) or Path.cwd()
    if explicit is not None:
        p = Path(explicit).expanduser()
        if not p.is_absolute():
            p = (Path(workspace).expanduser().resolve() / p).resolve()
        else:
            p = p.resolve()
        return [p] if p.is_file() else [p]
    return models_config_paths(workspace)


def resolve_models_config_path(settings: Any) -> Path | None:
    """Primary (highest priority) models.json path for display/status."""
    paths = resolve_models_config_paths(settings)
    return paths[-1] if paths else None


def load_merged_models_registry(settings: Any) -> ModelRegistry | None:
    """Load and merge layered models.json (+ optional MODELS_JSON blob)."""
    blob = load_models_json_blob(getattr(settings, "models_json", None))
    reg: ModelRegistry | None = None
    for path in resolve_models_config_paths(settings):
        layer = load_models_config(path)
        reg = merge_model_registries(reg, layer)
    reg = merge_model_registries(reg, blob)
    return reg


def registry_from_settings(settings: Any) -> ModelRegistry:
    """Build registry from Settings, preferring multi-model config files."""
    from synapse.config import Settings  # local import to avoid cycles

    assert isinstance(settings, Settings)
    reg = load_merged_models_registry(settings)
    if reg is not None:
        selected = (getattr(settings, "active_model", None) or "").strip()
        if not selected:
            candidate = (settings.model or "").strip()
            if candidate in reg.profiles:
                selected = candidate
        if selected:
            if selected in reg.profiles:
                reg.default = selected
            elif ":" in selected:
                reg.profiles[selected] = ModelProfile(name=selected, model=selected)
                reg.default = selected
        return reg

    name = settings.model
    return ModelRegistry(
        profiles={
            name: ModelProfile(
                name=name,
                model=settings.model,
                api_key=settings.openai_api_key or settings.anthropic_api_key,
                base_url=settings.openai_base_url,
                enable_thinking=settings.enable_thinking,
                reasoning_effort=settings.reasoning_effort,
                parallel_tool_calls=settings.parallel_tool_calls,
            )
        },
        default=name,
    )


def apply_models_config_to_settings(settings: Any) -> Any:
    """Resolve layered models.json and sync selected profile into Settings."""
    reg = load_merged_models_registry(settings)
    if reg is None:
        return settings

    paths = resolve_models_config_paths(settings)
    primary = paths[-1] if paths else None
    if primary is not None and getattr(settings, "models_config_path", None) != primary:
        settings = settings.model_copy(update={"models_config_path": primary})

    selected = (getattr(settings, "active_model", None) or "").strip()
    if not selected:
        candidate = (settings.model or "").strip()
        if candidate in reg.profiles:
            selected = candidate
        else:
            selected = reg.default
    if selected not in reg.profiles and ":" not in selected:
        selected = reg.default
    profile = reg.get(selected)

    updates: dict[str, Any] = {
        "active_model": profile.name,
        "model": profile.model,
    }
    if profile.base_url:
        updates["openai_base_url"] = profile.base_url
    if profile.enable_thinking is not None:
        updates["enable_thinking"] = bool(profile.enable_thinking)
    if profile.reasoning_effort:
        updates["reasoning_effort"] = profile.reasoning_effort
    if profile.parallel_tool_calls is not None:
        updates["parallel_tool_calls"] = bool(profile.parallel_tool_calls)

    # Prefer keys from models.json so the agent can run without .env.
    key = profile.resolved_api_key()
    if key:
        if str(profile.model).startswith("anthropic:"):
            updates["anthropic_api_key"] = key
            updates["openai_api_key"] = None
        else:
            updates["openai_api_key"] = key
            updates["anthropic_api_key"] = None
    else:
        updates["openai_api_key"] = None
        updates["anthropic_api_key"] = None

    return settings.model_copy(update=updates)


def build_model_from_settings(settings: Any, *, model_name: str | None = None):
    """Convenience: registry + construct selected model."""
    reg = registry_from_settings(settings)
    selected = model_name or getattr(settings, "active_model", None) or reg.default
    profile = reg.get(selected)
    return reg, reg.build_chat_model(
        selected,
        fallback_api_key=settings_fallback_api_key(settings, profile.model),
        fallback_base_url=settings.openai_base_url,
        fallback_enable_thinking=settings.enable_thinking,
        fallback_reasoning_effort=settings.reasoning_effort,
        fallback_parallel_tool_calls=settings.parallel_tool_calls,
        fallback_stream_chunk_timeout=getattr(
            settings, "stream_chunk_timeout", None
        ),
        # Session Settings always win over profile defaults.
        enable_thinking=bool(settings.enable_thinking),
        reasoning_effort=settings.reasoning_effort,
    )




def model_provider(model: str | None) -> str:
    """Return provider prefix (openai/anthropic/...) or empty string."""
    text = (model or "").strip()
    if ":" not in text:
        return ""
    return text.split(":", 1)[0].strip().casefold()


def settings_fallback_api_key(settings: Any, model: str | None = None) -> str | None:
    """Pick settings-level API key for a model provider.

    Profile plaintext / api_key_env always wins earlier via resolved_api_key().
    This only supplies the fallback when the profile has no key.
    """
    provider = model_provider(model or getattr(settings, "model", None))
    openai_key = getattr(settings, "openai_api_key", None)
    anthropic_key = getattr(settings, "anthropic_api_key", None)
    if provider == "anthropic":
        return anthropic_key
    if provider == "openai":
        return openai_key
    return openai_key or anthropic_key


def apply_profile_to_settings(
    settings: Any,
    profile: ModelProfile,
    *,
    seed_thinking: bool = True,
) -> Any:
    """Apply a model profile's identity + credentials onto Settings.

    Thinking defaults are seeded only when ``seed_thinking`` is True (model switch).
    Credentials always follow the selected profile so switching models cannot keep
    the previous provider's key as the active credential source.
    """
    settings.active_model = profile.name
    settings.model = profile.model
    if profile.base_url:
        # Shared transport field used as OpenAI-compatible / Anthropic base_url source.
        settings.openai_base_url = profile.base_url

    key = profile.resolved_api_key()
    provider = model_provider(profile.model)
    if key:
        if provider == "anthropic":
            settings.anthropic_api_key = key
            settings.openai_api_key = None
        else:
            # openai + generic OpenAI-compatible providers
            settings.openai_api_key = key
            settings.anthropic_api_key = None
    else:
        # Clear both so stale keys from a previous profile cannot leak.
        settings.openai_api_key = None
        settings.anthropic_api_key = None

    if seed_thinking:
        if profile.enable_thinking is not None:
            settings.enable_thinking = bool(profile.enable_thinking)
        if profile.reasoning_effort:
            settings.reasoning_effort = profile.reasoning_effort
        if profile.parallel_tool_calls is not None:
            settings.parallel_tool_calls = bool(profile.parallel_tool_calls)
    return settings

def short_model_id(model: str | None) -> str:
    """Strip provider prefix: ``openai:deepseek-v4-pro`` -> ``deepseek-v4-pro``."""
    text = (model or "").strip()
    if ":" in text:
        text = text.split(":", 1)[1]
    return text or "model"


def settings_thinking_label(settings: Any) -> str:
    """Current effective thinking label from Settings (runtime override aware)."""
    if getattr(settings, "enable_thinking", True) is False:
        return "off"
    effort = getattr(settings, "reasoning_effort", None)
    if effort:
        return str(effort)
    return "on"


def format_model_status(settings: Any) -> str:
    """Status-bar label: ``deepseek-v4-pro · high``."""
    model = short_model_id(str(getattr(settings, "model", "") or ""))
    return f"{model} · {settings_thinking_label(settings)}"


def apply_thinking_to_settings(
    settings: Any,
    raw: str,
    *,
    allowed: list[str] | None = None,
) -> str:
    """Apply a thinking level token onto settings. Returns canonical label.

    Accepts off|minimal|low|medium|high|max (and aliases).
    When ``allowed`` is provided, the level must be in that catalog.
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("empty thinking level")
    level = normalize_thinking_level(text)
    if not level:
        raise ValueError(f"unknown thinking level: {raw}")
    catalog = list(allowed) if allowed is not None else default_thinking_levels()
    if level not in catalog:
        raise ValueError(
            f"thinking level {level!r} not allowed; choose one of: {', '.join(catalog)}"
        )
    if level == "off":
        settings.enable_thinking = False
        return "off"
    settings.enable_thinking = True
    settings.reasoning_effort = level
    return level


def is_thinking_token(raw: str) -> bool:
    """True if token looks like a thinking level (for /model parsing)."""
    text = (raw or "").strip().casefold()
    if text in {
        "off",
        "false",
        "0",
        "disabled",
        "none",
        "no",
        "on",
        "true",
        "1",
        "enabled",
        "yes",
        *THINKING_LEVELS,
        "min",
        "med",
        "mid",
        "maximum",
        "xhigh",
        "ultra",
        "highest",
    }:
        return True
    return normalize_thinking_level(text) in THINKING_LEVELS
