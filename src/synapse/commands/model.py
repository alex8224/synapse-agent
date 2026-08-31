"""Model selection and thinking slash-command handler."""
from __future__ import annotations

from collections.abc import Callable
from copy import copy
from pathlib import Path
from typing import Any

from synapse.commands.result import SlashResult
from synapse.models.registry import registry_from_settings


def handle_model(
    args: list[str],
    *,
    settings: Any,
    agent: Any,
    project_root: Path,
    thread_id: str | None = None,
    apply_thinking_inplace: Callable[[Any, Any, str], bool],
    rebuild_agent: Callable[..., Any],
    persist_model_binding: Callable[[Any, str | None], str | None],
    mcp_attach_pending: Callable[[Any], bool],
    defer_persist: bool = False,
) -> SlashResult:
    from synapse.models.registry import (
        apply_thinking_to_settings,
        format_model_status,
        is_thinking_token,
        settings_thinking_label,
    )

    working_settings = copy(settings) if defer_persist else settings
    reg = registry_from_settings(working_settings)
    cfg_path = getattr(settings, "models_config_path", None)
    active = (
        getattr(agent, "_coding_model_profile", None)
        or working_settings.active_model
        or reg.default
    )
    allowed = reg.allowed_thinking_levels(active)
    allowed_help = "|".join(allowed) if allowed else "off|low|medium|high|max"

    if not args:
        lines = [
            f"active={active}",
            f"display={format_model_status(settings)}",
            f"thinking={settings_thinking_label(settings)}",
            f"thinking_levels={', '.join(allowed)}",
        ]
        if cfg_path:
            lines.append(f"config={cfg_path}")
        for name in reg.list_names():
            p = reg.get(name)
            mark = "*" if name == (settings.active_model or reg.default) else " "
            base = f" base={p.base_url}" if p.base_url else ""
            levels = reg.allowed_thinking_levels(name)
            lines.append(
                f"{mark} {name} -> {p.model} default_thinking={p.thinking_label()}"
                f" levels=[{', '.join(levels)}]{base}"
            )
        lines.append("usage: /model <alias|provider:model> [thinking]")
        lines.append(f"       /model thinking <{allowed_help}>")
        lines.append("       /model <alias> thinking <level>")
        lines.append(
            "note: thinking_levels are independent of model identity; "
            "session thinking overrides profile default"
        )
        return SlashResult(handled=True, lines=lines)

    # /model thinking <level>
    if args[0].strip().casefold() in {"thinking", "effort", "reasoning"}:
        if len(args) < 2:
            return SlashResult(
                handled=True,
                lines=[f"usage: /model thinking <{allowed_help}>"],
                error=True,
            )
        try:
            label = apply_thinking_to_settings(working_settings, args[1], allowed=allowed)
        except ValueError as exc:
            return SlashResult(handled=True, lines=[str(exc)], error=True)
        persist_error = None
        model_name = working_settings.active_model or reg.default
        new_agent = None
        note = ""
        if not defer_persist and apply_thinking_inplace(working_settings, agent, model_name):
            note = " (live, no rebuild)"
        else:
            try:
                new_agent = rebuild_agent(
                    working_settings,
                    project_root=project_root,
                    model_name=model_name,
                    agent=agent,
                    defer_mcp_reconnect=True,
                )
            except Exception as exc:  # noqa: BLE001
                lines = [f"thinking update failed: {exc}"]
                if persist_error:
                    lines.append(persist_error)
                return SlashResult(handled=True, lines=lines, error=True)
        if not defer_persist:
            persist_error = persist_model_binding(working_settings, thread_id)
        lines = [f"thinking set to {label}{note}  ({format_model_status(working_settings)})"]
        if persist_error:
            lines.append(persist_error)
        return SlashResult(
            handled=True,
            lines=lines,
            agent=new_agent,
            settings_changed=True,
            candidate_settings=working_settings if defer_persist else None,
            error=bool(persist_error),
            mcp_attach_pending=bool(new_agent is not None and mcp_attach_pending(working_settings)),
        )

    target = args[0].strip()
    try:
        profile = reg.get(target)
    except KeyError as exc:
        return SlashResult(handled=True, lines=[str(exc)], error=True)

    from synapse.models.registry import apply_profile_to_settings

    apply_profile_to_settings(working_settings, profile, seed_thinking=True)

    # /model <alias> high
    # /model <alias> thinking high
    think_raw: str | None = None
    if len(args) >= 3 and args[1].strip().casefold() in {
        "thinking",
        "effort",
        "reasoning",
    }:
        think_raw = args[2]
    elif len(args) >= 2 and is_thinking_token(args[1]):
        think_raw = args[1]
    elif len(args) >= 2 and args[1].strip().casefold() not in {
        "thinking",
        "effort",
        "reasoning",
    }:
        # Second arg present but not a known thinking token -> error for clarity
        return SlashResult(
            handled=True,
            lines=[
                f"unknown thinking level: {args[1]}",
                f"usage: /model <alias> [{allowed_help}]",
            ],
            error=True,
        )
    persist_error = None

    if think_raw is not None:
        try:
            apply_thinking_to_settings(
                working_settings,
                think_raw,
                allowed=reg.allowed_thinking_levels(profile.name),
            )
        except ValueError as exc:
            return SlashResult(handled=True, lines=[str(exc)], error=True)

    try:
        new_agent = rebuild_agent(
            working_settings,
            project_root=project_root,
            model_name=profile.name,
            agent=agent,
            defer_mcp_reconnect=True,
        )
    except Exception as exc:  # noqa: BLE001
        lines = [f"model switch failed: {exc}"]
        if persist_error:
            lines.append(persist_error)
        return SlashResult(
            handled=True,
            lines=lines,
            error=True,
        )
    if not defer_persist:
        persist_error = persist_model_binding(working_settings, thread_id)
    mcp_attach_pending = mcp_attach_pending(working_settings)
    lines = [f"model switched to {profile.name}  ({format_model_status(working_settings)})"]
    if persist_error:
        lines.append(persist_error)
    return SlashResult(
        handled=True,
        lines=lines,
        agent=new_agent,
        settings_changed=True,
        candidate_settings=working_settings if defer_persist else None,
        error=bool(persist_error),
        mcp_attach_pending=mcp_attach_pending,
    )
