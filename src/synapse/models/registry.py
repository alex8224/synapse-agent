"""Multi-model registry built on top of deepagents/LangChain model assembly.

Preferred configuration: `.synapse/models.json`
Secrets stay in env / `.env` via `api_key_env`.

Top-level fields:
  default, models,
  thinking_levels (array): allowed session thinking levels, shared by models
  default_thinking: optional global default when a profile omits thinking

Profile fields:
  model, api_key_env, base_url, websocket,
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

import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from synapse.integrations.llm_openai_compat import (
    deepseek_thinking_kwargs,
    enable_openai_compat_reasoning_patch,
    enable_responses_reasoning_patch,
)
from synapse.models.config import (
    DEFAULT_MODELS_CONFIG_REL,
    DEFAULT_THINKING_LEVELS,
    _coerce_params,
    apply_context_window_to_model,
    default_thinking_levels,
    expand_env_string,
    normalize_thinking_level,
    parse_context_window,
    parse_optional_bool,
    parse_thinking_config,
    parse_thinking_levels,
)
from synapse.models.helpers import (
    apply_profile_to_settings as _apply_profile_to_settings,
)
from synapse.models.helpers import (
    apply_thinking_to_settings as _apply_thinking_to_settings,
)
from synapse.models.helpers import format_model_status as _format_model_status
from synapse.models.helpers import is_thinking_token as _is_thinking_token
from synapse.models.helpers import model_provider as _model_provider
from synapse.models.helpers import model_supports_image_input as _model_supports_image_input
from synapse.models.helpers import settings_fallback_api_key, short_model_id
from synapse.models.helpers import settings_thinking_label as _settings_thinking_label
from synapse.models.profile import ModelProfile

apply_profile_to_settings = _apply_profile_to_settings
apply_thinking_to_settings = _apply_thinking_to_settings
format_model_status = _format_model_status
is_thinking_token = _is_thinking_token
model_provider = _model_provider
model_supports_image_input = _model_supports_image_input
settings_thinking_label = _settings_thinking_label

_init_chat_model: Any | None = None


def __getattr__(name: str) -> Any:
    """Lazily resolve :data:`init_chat_model`.

    ``langchain.chat_models`` costs ~1.3s to import and is only needed when a
    model client is actually built, so it is kept out of the module import
    (the TUI startup path pulls in this module via the /model picker).
    """
    global _init_chat_model
    if name == "init_chat_model":
        if _init_chat_model is None:
            from langchain.chat_models import init_chat_model

            _init_chat_model = init_chat_model
        return _init_chat_model
    raise AttributeError(name)


def _resolve_init_chat_model() -> Callable[..., Any]:
    """Return the lazily imported model factory for internal call sites.

    Module-level ``__getattr__`` only serves attribute access from outside the
    module. Bare global-name lookup inside this module bypasses it, so resolve
    the factory explicitly before building a model client.
    """
    if factory := globals().get("init_chat_model"):
        return factory
    return __getattr__("init_chat_model")


def _resolve_turbo_proxy_url(profile: ModelProfile, fallback: str | None = None) -> str:
    """Resolve the headroom-turbo proxy base URL for a profile.

    Priority: profile ``turbo_base_url`` → explicit fallback (Settings) →
    ``SYNAPSE_TURBO_PROXY_URL`` env → the local default ``http://localhost:8787/v1``.
    """
    return (
        profile.turbo_base_url
        or fallback
        or os.environ.get("SYNAPSE_TURBO_PROXY_URL")
        or "http://localhost:8787/v1"
    )


def _turbo_upstream_origin(base_url: str) -> str:
    """Reduce an upstream base URL to scheme://host[:port] for the proxy.

    Headroom's OpenAI chat path appends ``/v1/chat/completions`` to the
    ``x-headroom-base-url`` value itself, so forwarding the full base URL
    (e.g. ``http://host/v1``) would produce a duplicated ``/v1/v1/...`` path.
    Sending the bare origin keeps sub-path routing correct for standard
    OpenAI-compatible endpoints.
    """
    parsed = urlparse(str(base_url).strip())
    if not parsed.scheme or not parsed.hostname:
        return str(base_url).rstrip("/")
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}"


@dataclass
class ModelRegistry:
    """Alias catalog for chat models + shared thinking level catalog."""

    profiles: dict[str, ModelProfile]
    default: str
    # Shared OpenAI-compatible request headers. Profile headers take precedence.
    headers: dict[str, str] = field(default_factory=dict)
    # Allowed thinking levels for the session (/model thinking ...).
    thinking_levels: list[str] = field(default_factory=default_thinking_levels)
    # Optional global default when a profile omits thinking config.
    default_thinking: str | None = None
    # Independent image-to-text model configuration from models.json.
    vision_model: dict[str, Any] | None = None

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
        fallback_websocket: bool = False,
        fallback_stream_chunk_timeout: float | None = None,
        fallback_turbo: bool = False,
        fallback_turbo_proxy_url: str | None = None,
        enable_thinking: bool | None = None,
        reasoning_effort: str | None = None,
        progress: Callable[[str], None] | None = None,
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
        from synapse.observability.startup_trace import span

        profile = self.get(name)
        kwargs: dict[str, Any] = dict(profile.extra or {})
        model_name = profile.model
        configured_headers = _merge_headers(self.headers, profile.headers)

        api_key = profile.resolved_api_key() or fallback_api_key
        base_url = profile.base_url or fallback_base_url
        oauth_provider = None
        oauth_headers: dict[str, str] = {}
        if profile.auth == "openai_oauth":
            if not model_name.startswith("openai:"):
                raise ValueError("auth=openai_oauth requires an openai: model profile")
            from synapse.integrations.openai_oauth import (
                OPENAI_CODEX_BASE_URL,
                OpenAIOAuthTokenProvider,
            )

            oauth_provider = OpenAIOAuthTokenProvider()
            # Resolve once so a missing/expired login is reported during model startup,
            # and preserve the account id required by the ChatGPT Codex backend.
            api_key = oauth_provider.access_token()
            # OAuth grants are only valid for the first-party Codex backend.
            # Never inherit profile/global base_url: doing so could leak the bearer
            # token to a project-configured third-party endpoint.
            base_url = OPENAI_CODEX_BASE_URL
            account_id = oauth_provider.account_id()
            if not account_id:
                raise ValueError(
                    "OpenAI Codex OAuth credentials are missing ChatGPT-Account-Id; "
                    "run: synapse auth openai login"
                )
            oauth_headers["ChatGPT-Account-Id"] = account_id
            oauth_headers["originator"] = "synapse"

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
        websocket = fallback_websocket if profile.websocket is None else profile.websocket

        if model_name.startswith("openai:"):
            if progress is not None:
                progress("loading OpenAI SDK")
            with span("model:openai_compat_patch"):
                enable_openai_compat_reasoning_patch()
                enable_responses_reasoning_patch()
            # Turbo mode: route through the headroom-turbo proxy and forward the
            # original upstream via x-headroom-base-url so the proxy compresses
            # and relays. Enabled globally (Settings.turbo) or per profile
            # (profile.turbo). OAuth-backed Codex is excluded (its base_url is
            # pinned to the first-party backend and must never be redirected).
            turbo_enabled = bool(fallback_turbo or profile.turbo)
            if turbo_enabled and base_url and oauth_provider is None:
                configured_headers = _merge_headers(
                    configured_headers,
                    {"x-headroom-base-url": _turbo_upstream_origin(base_url)},
                )
                base_url = _resolve_turbo_proxy_url(profile, fallback_turbo_proxy_url)
            if base_url:
                kwargs["base_url"] = str(base_url).rstrip("/")
            if api_key:
                kwargs["api_key"] = api_key
            # Later sources override earlier sources by HTTP header name.
            # OAuth headers are protocol credentials and cannot be overridden by
            # configuration; custom fingerprint headers remain otherwise unrestricted.
            headers = _merge_headers(
                dict(kwargs.get("default_headers") or {}), configured_headers, oauth_headers
            )
            if headers:
                kwargs["default_headers"] = headers
            kwargs.setdefault("use_responses_api", bool(websocket) or oauth_provider is not None)
            if websocket or oauth_provider is not None:
                kwargs["use_responses_api"] = True
            if oauth_provider is not None:
                # The first-party Codex backend rejects persisted Responses.
                kwargs["store"] = False
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
            # Codex OAuth uses OpenAI Responses semantics. Never send the
            # DeepSeek-compatible `extra_body.thinking` extension to that backend.
            think_kwargs = (
                {"reasoning_effort": resolved_effort}
                if oauth_provider is not None and resolved_enable
                else {}
                if oauth_provider is not None
                else deepseek_thinking_kwargs(
                    enabled=bool(resolved_enable),
                    reasoning_effort=str(resolved_effort or "high"),
                )
            )
            extra_body = dict(think_kwargs.get("extra_body") or {})
            user_body = dict(profile.extra_body or {})
            if oauth_provider is not None:
                # Defense in depth: users may have supplied this extension through
                # profile.extra/default extra_body as well as profile.extra_body.
                extra_body.pop("thinking", None)
                user_body.pop("thinking", None)
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
            if oauth_provider is not None:
                existing_body.pop("thinking", None)
            kwargs["extra_body"] = existing_body
            # Build an async-only OpenAI path. An async API-key callable tells
            # ChatOpenAI not to construct its otherwise eager sync OpenAI client.
            from synapse.integrations.http_clients import build_openai_async_http_client

            if progress is not None:
                progress("creating async model client")
            async_client = build_openai_async_http_client(
                timeout=kwargs.get("timeout"),
                proxy=kwargs.pop("openai_proxy", None),
            )

            async def _async_api_key() -> str:
                if oauth_provider is not None:
                    import asyncio

                    return await asyncio.to_thread(oauth_provider.access_token)
                return str(api_key or "")

            kwargs["api_key"] = _async_api_key
            kwargs["http_async_client"] = async_client
            kwargs.setdefault("http_socket_options", ())
        elif model_name.startswith("anthropic:"):
            if progress is not None:
                progress("loading Anthropic SDK")
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
            from synapse.integrations.http_clients import enable_anthropic_long_keepalive_defaults

            enable_anthropic_long_keepalive_defaults()

        if progress is not None and not model_name.startswith("openai:"):
            progress("creating async model client")
        try:
            with span("model:init_chat_model"):
                if model_name.startswith("openai:") and websocket:
                    from synapse.integrations.llm_openai_websocket import (
                        ResponsesWebSocketChatOpenAI,
                    )

                    chat_model = ResponsesWebSocketChatOpenAI(
                        model=short_model_id(model_name),
                        **kwargs,
                    )
                else:
                    chat_model = _resolve_init_chat_model()(model_name, **kwargs)
        except Exception:
            if model_name.startswith("openai:"):
                from synapse.runtime.async_runtime import get_async_runtime

                get_async_runtime().close_connection(async_client)
            raise
        if model_name.startswith("openai:"):
            chat_model._coding_http_async_client = async_client
            chat_model._coding_async_only = True
            chat_model._coding_websocket = bool(websocket)
            chat_model._synapse_openai_oauth = oauth_provider is not None
            if websocket:
                from synapse.runtime.async_runtime import get_async_runtime

                get_async_runtime().track_connection(chat_model)
        return apply_context_window_to_model(chat_model, profile.context_window)


def _merge_headers(*sources: dict[str, str]) -> dict[str, str]:
    """Merge headers case-insensitively while keeping the latest spelling/value."""
    merged: dict[str, str] = {}
    names: dict[str, str] = {}
    for source in sources:
        for name, value in source.items():
            normalized = name.casefold()
            if previous := names.get(normalized):
                merged.pop(previous, None)
            names[normalized] = name
            merged[name] = value
    return merged


def _parse_headers(value: Any, *, field_name: str) -> dict[str, str]:
    """Validate JSON request headers and expand environment placeholders in values."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be an object of string headers")
    headers: dict[str, str] = {}
    for raw_name, raw_value in value.items():
        name = str(raw_name).strip()
        if not name or any(char in name for char in "\r\n:"):
            raise ValueError(f"{field_name} contains an invalid header name")
        if not isinstance(raw_value, str):
            raise ValueError(f"{field_name}.{name} must be a string")
        resolved = str(expand_env_string(raw_value)).strip()
        if "\r" in resolved or "\n" in resolved:
            raise ValueError(f"{field_name}.{name} contains an invalid header value")
        headers[name] = resolved
    return headers


def _profiles_from_mapping(data: dict[str, Any]) -> ModelRegistry:
    raw_models = data.get("models") or {}
    if not isinstance(raw_models, dict) or not raw_models:
        raise ValueError("models config must contain a non-empty 'models' object")
    global_headers = _parse_headers(data.get("headers"), field_name="headers")

    top_levels = parse_thinking_levels(data.get("thinking_levels"))
    thinking_levels = top_levels or default_thinking_levels()
    default_thinking = normalize_thinking_level(data.get("default_thinking"))
    vision_model = data.get("vision_model")
    if vision_model is not None and not isinstance(vision_model, (str, dict)):
        raise ValueError("vision_model must be an object or profile name")
    if vision_model is None and isinstance(raw_models.get("vision_model"), dict):
        # Backward-compatible convenience: allow the vision endpoint to live
        # alongside primary model profiles under models.vision_model.
        vision_model = dict(raw_models["vision_model"])

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

        provider = cfg.get("provider")
        if provider is not None:
            provider = str(provider).strip().casefold() or None
        wire_api = cfg.get("wire_api")
        if wire_api is not None:
            wire_api = str(wire_api).strip().casefold()
            if wire_api not in {"chat", "responses"}:
                raise ValueError(
                    f"model profile {name!r} wire_api must be 'chat' or 'responses'"
                )

        headers = _parse_headers(cfg.get("headers"), field_name=f"model profile {name!r} headers")

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

        image_input = cfg.get("image_input")
        if image_input is None and isinstance(cfg.get("capabilities"), dict):
            image_input = cfg["capabilities"].get("image_input")
        image_input = parse_optional_bool(image_input)
        websocket = parse_optional_bool(cfg.get("websocket"), field_name="websocket")

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
            auth=str(cfg.get("auth") or "").strip().casefold() or None,
            base_url=base_url,
            provider=provider,
            wire_api=wire_api,
            headers=headers,
            context_window=context_window,
            enable_thinking=enable_thinking,
            reasoning_effort=reasoning_effort,
            thinking_levels=tuple(profile_levels) if profile_levels else None,
            parallel_tool_calls=None if parallel is None else bool(parallel),
            websocket=websocket,
            image_input=image_input,
            turbo=bool(cfg.get("turbo") or False),
            turbo_base_url=cfg.get("turbo_base_url") or None,
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
        headers=global_headers,
        vision_model=dict(vision_model) if vision_model else None,
        thinking_levels=thinking_levels,
        default_thinking=default_thinking,
    )


def load_models_config(path: Path | str | None) -> ModelRegistry | None:
    """Load models JSON if path exists; return None when unset/missing.

    Raises:
        ValueError: If the file does not contain valid JSON or a valid model registry.
    """
    if path is None:
        return None
    p = Path(path).expanduser()
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("root must be an object")
        return _profiles_from_mapping(data)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid models config JSON in {p}: {exc}") from exc
    except (OSError, ValueError) as exc:
        raise ValueError(f"invalid models config in {p}: {exc}") from exc


def load_models_json_blob(blob: str | None) -> ModelRegistry | None:
    """Load an inline MODELS_JSON registry, if configured."""
    if not blob or not blob.strip():
        return None
    try:
        data = json.loads(blob)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid MODELS_JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("MODELS_JSON root must be an object")
    try:
        return _profiles_from_mapping(data)
    except ValueError as exc:
        raise ValueError(f"invalid MODELS_JSON: {exc}") from exc


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
        auth=override.auth if override.auth not in (None, "") else base.auth,
        base_url=override.base_url if override.base_url not in (None, "") else base.base_url,
        provider=(override.provider or base.provider),
        wire_api=(override.wire_api or base.wire_api),
        headers=_merge_headers(base.headers, override.headers),
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
        websocket=(
            override.websocket if override.websocket is not None else base.websocket
        ),
        image_input=(
            override.image_input
            if override.image_input is not None
            else base.image_input
        ),
        turbo=(override.turbo or base.turbo),
        turbo_base_url=(override.turbo_base_url or base.turbo_base_url),
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
        headers=_merge_headers(base.headers, override.headers),
        vision_model=(
            {**base.vision_model, **override.vision_model}
            if base.vision_model and override.vision_model
            else override.vision_model or base.vision_model
        ),
        thinking_levels=thinking_levels,
        default_thinking=default_thinking,
    )


def default_models_config_path(workspace: Path | str | None = None) -> Path:
    """Canonical project path: <workspace>/.synapse/models.json."""
    base = Path(workspace).expanduser().resolve() if workspace is not None else Path.cwd()
    return (base / DEFAULT_MODELS_CONFIG_REL).resolve()


def resolve_models_config_paths(settings: Any) -> list[Path]:
    """All models.json files that participate in the merge (user → project).

    If ``settings.models_config_path`` is set, only that explicit file is used.
    """
    from synapse.settings.config_paths import models_config_paths

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
                websocket=getattr(settings, "openai_websocket", False),
                image_input=None,
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
    # Keep OPENAI_WEBSOCKET as the global/legacy fallback. A profile-local
    # websocket value is resolved directly from ModelProfile when that model is
    # built; copying it into Settings would make later profiles without an
    # explicit value inherit the previous model's transport.

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


def model_cache_key(settings: Any, *, model_name: str | None = None) -> str:
    """Stable key for reusing one configured ChatModel within an agent session."""
    reg = registry_from_settings(settings)
    selected = model_name or getattr(settings, "active_model", None) or reg.default
    profile = reg.get(selected)
    api_key = profile.resolved_api_key() or settings_fallback_api_key(settings, profile.model)
    payload = {
        "selected": selected,
        "model": profile.model,
        "auth": profile.auth,
        "provider": profile.provider,
        "wire_api": profile.wire_api,
        "base_url": profile.base_url or getattr(settings, "openai_base_url", None),
        "turbo": bool(getattr(settings, "turbo", False)) or profile.turbo,
        "turbo_base_url": profile.turbo_base_url
        or getattr(settings, "turbo_proxy_url", None),
        "api_key_sha256": hashlib.sha256((api_key or "").encode()).hexdigest(),
        "enable_thinking": bool(getattr(settings, "enable_thinking", True)),
        "reasoning_effort": getattr(settings, "reasoning_effort", None),
        "parallel_tool_calls": getattr(settings, "parallel_tool_calls", True),
        "websocket": (
            profile.websocket
            if profile.websocket is not None
            else getattr(settings, "openai_websocket", False)
        ),
        "stream_chunk_timeout": getattr(settings, "stream_chunk_timeout", None),
        "context_window": profile.context_window,
        "headers": {**reg.headers, **profile.headers},
        "extra": profile.extra,
        "model_kwargs": profile.model_kwargs,
        "extra_body": profile.extra_body,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def build_model_from_settings(
    settings: Any,
    *,
    model_name: str | None = None,
    progress: Callable[[str], None] | None = None,
):
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
        fallback_websocket=getattr(settings, "openai_websocket", False),
        fallback_stream_chunk_timeout=getattr(
            settings, "stream_chunk_timeout", None
        ),
        fallback_turbo=bool(getattr(settings, "turbo", False)),
        fallback_turbo_proxy_url=getattr(settings, "turbo_proxy_url", None),
        # Session Settings always win over profile defaults.
        enable_thinking=bool(settings.enable_thinking),
        reasoning_effort=settings.reasoning_effort,
        progress=progress,
    )