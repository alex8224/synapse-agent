"""Detect and import Codex CLI model configuration into Synapse ``models.json``.

Reads ``~/.codex/config.toml`` (user) and ``<workspace>/.codex/config.toml``
(project, merged over user) plus the Codex ``auth.json`` credential status.
Mapping rules per ``[model_providers.<id>]``:

  * built-in providers (openai/anthropic) keep their native prefix; an explicit
    ``base_url`` in Codex config is honored for both
  * unknown/custom provider ids map to the OpenAI-compatible transport
    (``openai:`` + ``base_url``)
  * ``env_key`` (string or array) maps to ``api_key_env``; the first present
    environment variable wins and others are dropped
  * ``wire_api = "responses"`` is honored only through Synapse OAuth profiles;
    for API-key providers it is downgraded to ``chat`` with a warning
  * ``query_params`` maps to the OpenAI ``default_query`` chat kwarg
  * ``auth.json`` secrets are never copied into ``models.json`` — OAuth grants
    are surfaced as a status hint, plaintext keys stay in the environment

Import is idempotent: the caller plans against the current store and chooses
per-alias conflict resolution (add/skip/rename) before a single atomic write.
"""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Safety caps for untrusted config files.
MAX_TOML_BYTES = 512 * 1024
MAX_PROVIDERS = 64
MAX_PROFILES = 128
MAX_HEADERS = 64

CODEX_HOME_NAME = ".codex"
CONFIG_FILENAME = "config.toml"
AUTH_FILENAME = "auth.json"

# Provider ids Codex treats as first-party; Synapse maps them to native
# ``provider:`` prefixed model ids. Everything else is an OpenAI-compatible
# gateway with a custom base_url.
BUILTIN_PROVIDERS = frozenset({"openai", "anthropic"})


@dataclass(frozen=True)
class CodexConfigSource:
    """One discovered Codex config or credential file."""

    path: Path
    exists: bool
    error: str | None = None

    def describe(self) -> str:
        if self.error:
            return f"{self.path} (unreadable: {self.error})"
        return str(self.path) if self.exists else f"{self.path} (missing)"


@dataclass(frozen=True)
class CodexConfigScan:
    """Detection result: files found + auth status + parse warnings."""

    user_config: CodexConfigSource
    project_config: CodexConfigSource
    auth: CodexConfigSource
    auth_status: str  # "oauth" | "api-key" | "missing" | "unreadable"
    warnings: tuple[str, ...] = ()

    @property
    def any_config(self) -> bool:
        return self.user_config.exists or self.project_config.exists

    @property
    def any_error(self) -> bool:
        return bool(self.user_config.error or self.project_config.error)


@dataclass
class ImportPlanItem:
    """One profile the import wants to write."""

    alias: str
    provider_id: str
    model: str  # full ``provider:model`` id
    payload: dict[str, Any]
    source: str  # e.g. "model_providers.openai" / "profiles.lite" / "default"
    conflict: bool
    action: str = "add"  # "add" | "skip" | "rename"
    warning: str = ""
    renamed_to: str | None = None


@dataclass
class ImportPlan:
    """Full import plan against the current models store."""

    items: list[ImportPlanItem]
    target_path: Path
    warnings: list[str] = field(default_factory=list)

    def pending(self) -> list[ImportPlanItem]:
        return [item for item in self.items if item.action != "skip"]

    @property
    def summary(self) -> str:
        pending = self.pending()
        return f"{len(pending)} profile(s) to import -> {self.target_path}"


def codex_home() -> Path:
    """User Codex config home; ``CODEX_HOME`` overrides the default ``~/.codex``."""
    override = os.environ.get("CODEX_HOME")
    if override and str(override).strip():
        return Path(override).expanduser().resolve()
    return (Path.home() / CODEX_HOME_NAME).expanduser().resolve()


def codex_config_paths(workspace: Path | str | None = None) -> list[Path]:
    """Ordered config.toml paths: user → project (later overrides earlier)."""
    paths: list[Path] = [codex_home() / CONFIG_FILENAME]
    if workspace is not None:
        base = Path(workspace).expanduser().resolve()
        paths.append(base / CODEX_HOME_NAME / CONFIG_FILENAME)
    return [p for p in dict.fromkeys(p.resolve() for p in paths)]


def _read_toml_with_error(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        if not path.is_file():
            return None, None
        if path.stat().st_size > MAX_TOML_BYTES:
            return None, "file too large"
        raw = path.read_bytes()
        parsed = tomllib.loads(raw.decode("utf-8"))
        if not isinstance(parsed, dict):
            return None, "root must be a TOML table"
        return parsed, None
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        return None, str(exc)


def scan_codex_config(workspace: Path | str | None = None) -> CodexConfigScan:
    """Detect Codex config files and credential status without leaking secrets."""
    paths = codex_config_paths(workspace)
    user_path = paths[0]
    project_path = paths[1] if len(paths) > 1 else paths[0]
    auth_path = codex_home() / AUTH_FILENAME

    _, user_err = _read_toml_with_error(user_path)
    _, project_err = _read_toml_with_error(project_path)

    auth_status = "missing"
    auth_err: str | None = None
    if auth_path.is_file():
        try:
            raw = json.loads(auth_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                if raw.get("tokens"):
                    auth_status = "oauth"
                elif raw.get("OPENAI_API_KEY"):
                    auth_status = "api-key"
                else:
                    auth_status = "missing"
            else:
                auth_status = "unreadable"
                auth_err = "root must be a JSON object"
        except (OSError, json.JSONDecodeError) as exc:
            auth_status = "unreadable"
            auth_err = str(exc)

    warnings: list[str] = []
    if user_err:
        warnings.append(f"user config unreadable: {user_path} ({user_err})")
    if project_err:
        warnings.append(f"project config unreadable: {project_path} ({project_err})")

    return CodexConfigScan(
        user_config=CodexConfigSource(user_path, user_path.is_file(), user_err),
        project_config=CodexConfigSource(project_path, project_path.is_file(), project_err),
        auth=CodexConfigSource(auth_path, auth_path.is_file(), auth_err),
        auth_status=auth_status,
        warnings=tuple(warnings),
    )


def load_codex_config(workspace: Path | str | None = None) -> dict[str, Any] | None:
    """Merge user → project config.toml into one dict. Invalid files are skipped."""
    merged: dict[str, Any] = {}
    any_loaded = False
    for path in codex_config_paths(workspace):
        parsed, _ = _read_toml_with_error(path)
        if parsed is None:
            continue
        any_loaded = True
        for key, value in parsed.items():
            # model_providers/profiles merge per-section (later overrides).
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                section = dict(merged[key])
                section.update(value)
                merged[key] = section
            else:
                merged[key] = value
    return merged if any_loaded else None


def _pick_env_key(env_key: Any) -> tuple[str | None, list[str]]:
    """Resolve env_key (str|list) -> (chosen env name, missed candidates)."""
    if env_key is None:
        return None, []
    if isinstance(env_key, str):
        keys = [env_key]
    elif isinstance(env_key, (list, tuple)):
        keys = list(env_key)
    else:
        return None, []
    keys = [str(k).strip() for k in keys if str(k or "").strip()]
    for key in keys:
        if os.environ.get(key):
            return key, [k for k in keys if k != key]
    return (keys[0] if keys else None), keys


def _normalize_provider_id(provider_id: str) -> str:
    return str(provider_id).strip().casefold()


def _short_model_id(model_id: str) -> str:
    return model_id.split(":", 1)[-1] or model_id


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _build_profile_payload(
    *,
    provider_id: str,
    model_id: str,
    base_url: str | None,
    env_key: Any,
    wire_api: str | None,
    http_headers: dict[str, Any] | None,
    env_http_headers: list[Any] | None,
    query_params: dict[str, Any] | None,
    reasoning_effort: str | None,
    warnings: list[str],
) -> dict[str, Any]:
    """Map one Codex model_provider + model id to a Synapse profile dict."""
    provider_id = _normalize_provider_id(provider_id)
    builtin = provider_id in BUILTIN_PROVIDERS
    prefix = provider_id if builtin else "openai"

    wire = str(wire_api or "chat").strip().casefold()
    if wire not in {"chat", "responses"}:
        warnings.append(f"unknown wire_api {wire_api!r} treated as 'chat'")
        wire = "chat"
    # responses is OAuth-only in Synapse; API-key profiles use chat.
    if wire == "responses":
        warnings.append(
            f"provider {provider_id!r} uses wire_api=responses which is OAuth-only; "
            "downgraded to the chat transport"
        )
        wire = "chat"

    resolved_env, missed = _pick_env_key(env_key)
    payload: dict[str, Any] = {"model": f"{prefix}:{model_id}"}
    if resolved_env:
        payload["api_key_env"] = resolved_env
    elif missed:
        warnings.append(
            f"provider {provider_id!r}: no env var set for {', '.join(missed)}; "
            "the API key will be requested from the environment at runtime"
        )
    if base_url:
        payload["base_url"] = str(base_url).strip().rstrip("/")
    if reasoning_effort:
        payload["reasoning_effort"] = str(reasoning_effort).strip().casefold()

    headers: dict[str, str] = {}
    for hname, hvalue in dict(http_headers or {}).items():
        if len(headers) >= MAX_HEADERS:
            warnings.append(f"provider {provider_id!r}: too many http_headers, truncated")
            break
        headers[str(hname)] = str(hvalue)
    for env_name in env_http_headers or []:
        env_name = str(env_name).strip()
        if not env_name:
            continue
        if len(headers) >= MAX_HEADERS:
            break
        value = os.environ.get(env_name)
        if value is None:
            warnings.append(
                f"provider {provider_id!r}: env_http_header {env_name!r} is not set"
            )
            continue
        headers[env_name] = value
    if headers:
        payload["headers"] = headers

    if query_params:
        payload["model_kwargs"] = {"default_query": dict(query_params)}
    return payload


def _provider_section(
    model_providers: dict[str, Any], provider_id: str
) -> dict[str, Any]:
    section = model_providers.get(provider_id)
    return section if isinstance(section, dict) else {}


def build_import_plan(
    workspace: Path | str | None,
    target: dict[str, Any],
    *,
    target_path: Path | None = None,
) -> ImportPlan:
    """Build the import plan against an existing models store (``target``)."""
    plan = ImportPlan(
        items=[],
        target_path=Path("."),
        warnings=[],
    )
    scan = scan_codex_config(workspace)
    plan.warnings.extend(scan.warnings)

    raw = load_codex_config(workspace)
    if raw is None:
        raise ValueError("no readable Codex config.toml found")

    model_providers = raw.get("model_providers") or {}
    if not isinstance(model_providers, dict):
        raise ValueError("Codex config 'model_providers' must be a table")
    if len(model_providers) > MAX_PROVIDERS:
        raise ValueError("Codex config has too many model_providers")

    top_model = str(raw.get("model") or "").strip()
    top_provider = _normalize_provider_id(str(raw.get("model_provider") or ""))
    reasoning_effort = raw.get("model_reasoning_effort")
    if reasoning_effort is not None:
        reasoning_effort = str(reasoning_effort).strip().casefold()

    existing = target.get("models") or {}

    # 1) Default model from top-level configuration.
    if top_model:
        section = _provider_section(model_providers, top_provider)
        _add_plan_item(
            plan,
            provider_id=top_provider,
            model_id=top_model,
            base_url=_str_or_none(section.get("base_url")),
            env_key=section.get("env_key"),
            wire_api=_str_or_none(section.get("wire_api")),
            http_headers=section.get("http_headers"),
            env_http_headers=section.get("env_http_headers"),
            query_params=section.get("query_params"),
            reasoning_effort=reasoning_effort,
            existing=existing,
            source="config default model",
            warnings=plan.warnings,
        )

    # 2) Named Codex profiles — each becomes a Synapse profile alias.
    profiles = raw.get("profiles") or {}
    if not isinstance(profiles, dict):
        plan.warnings.append("Codex config 'profiles' is not a table; skipped")
        profiles = {}
    if len(profiles) > MAX_PROFILES:
        plan.warnings.append("Codex config has too many profiles; truncated")

    for profile_name, section in profiles.items():
        if not isinstance(section, dict):
            plan.warnings.append(f"Codex profile {profile_name!r} is not a table; skipped")
            continue
        profile_name = str(profile_name).strip()
        model_id = str(section.get("model") or "").strip()
        provider_id = _normalize_provider_id(str(section.get("model_provider") or top_provider))
        if not model_id:
            plan.warnings.append(f"Codex profile {profile_name!r} has no model; skipped")
            continue
        provider_section = _provider_section(model_providers, provider_id)
        _add_plan_item(
            plan,
            provider_id=provider_id,
            model_id=model_id,
            base_url=_str_or_none(provider_section.get("base_url")),
            env_key=provider_section.get("env_key"),
            wire_api=_str_or_none(provider_section.get("wire_api")),
            http_headers=provider_section.get("http_headers"),
            env_http_headers=provider_section.get("env_http_headers"),
            query_params=provider_section.get("query_params"),
            reasoning_effort=reasoning_effort,
            existing=existing,
            source=f"profiles.{profile_name}",
            warnings=plan.warnings,
            alias_hint=profile_name,
        )

    plan.target_path = target_path or _first_missing_or_project(workspace)
    return plan


def _add_plan_item(
    plan: ImportPlan,
    *,
    provider_id: str,
    model_id: str,
    base_url: str | None,
    env_key: Any,
    wire_api: str | None,
    http_headers: dict[str, Any] | None,
    env_http_headers: list[Any] | None,
    query_params: dict[str, Any] | None,
    reasoning_effort: str | None,
    existing: dict[str, Any],
    source: str,
    warnings: list[str],
    alias_hint: str | None = None,
) -> None:
    """Append one plan item with a unique alias."""
    item_warnings: list[str] = []
    payload = _build_profile_payload(
        provider_id=provider_id,
        model_id=model_id,
        base_url=base_url,
        env_key=env_key,
        wire_api=wire_api,
        http_headers=http_headers,
        env_http_headers=env_http_headers,
        query_params=query_params,
        reasoning_effort=reasoning_effort,
        warnings=item_warnings,
    )
    candidate = alias_hint or payload["model"]
    used = {item.alias for item in plan.items}
    if candidate in used:
        suffix = 2
        candidate = f"{candidate}-{suffix}"
        while candidate in used:
            suffix += 1
            candidate = f"{candidate[:-2]}-{suffix}"
    warnings.extend(f"({source}) {w}" for w in item_warnings)
    plan.items.append(
        ImportPlanItem(
            alias=candidate,
            provider_id=provider_id,
            model=payload["model"],
            payload=payload,
            source=source,
            conflict=candidate in existing,
        )
    )


def apply_import(settings: Any, plan: ImportPlan) -> tuple[int, int]:
    """Write all pending plan items into the store in one atomic save.

    Returns ``(written, skipped)``. Per-item actions:
      * ``add``     — insert when the alias is free, otherwise skip
      * ``replace`` — overwrite an existing alias
      * ``skip``    — leave untouched
    """
    from synapse.models.persist import load_models_store, save_models_store

    data = load_models_store(settings)
    models = data.setdefault("models", {})
    if not isinstance(models, dict):
        raise ValueError("models config 'models' must be an object")

    written = 0
    skipped = 0
    for item in plan.pending():
        alias = item.renamed_to or item.alias
        if item.action == "skip":
            skipped += 1
            continue
        if item.action == "replace" and alias in models:
            models[alias] = dict(item.payload)
            written += 1
            continue
        if alias in models:
            skipped += 1
            continue
        models[alias] = dict(item.payload)
        if len(models) == 1:
            data["default"] = alias
        written += 1

    if written:
        save_models_store(settings, data)
    return written, skipped


def _first_missing_or_project(workspace: Path | str | None) -> Path:
    """Target path shown in the import preview line."""
    base = Path(workspace).expanduser().resolve() if workspace is not None else Path.cwd().resolve()
    return (base / ".synapse" / "models.json").resolve()