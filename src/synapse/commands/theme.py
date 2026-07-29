"""Theme selection slash-command handler."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from synapse.commands.helpers import markdown_escape
from synapse.commands.result import SlashResult


def handle_theme(
    args: list[str],
    *,
    settings: Any,
    project_root: Path,
) -> SlashResult:
    """List or switch UI themes; persist selection to user settings.json."""
    from synapse.ui.theme import (
        format_theme_list_lines,
        get_theme,
        list_theme_names,
        reload_theme_catalog,
        set_theme,
    )

    reload_theme_catalog(project_root)
    if not args or args[0].casefold() in {"list", "ls", "show"}:
        active = getattr(settings, "theme", None) or get_theme().name
        plain = format_theme_list_lines(active=active)
        from synapse.ui.theme import list_themes, theme_kind

        md = (
            "## Themes\n\n"
            f"**current**: `{active}`\n\n"
            "|   | Name | Label | Kind |\n|---|---|---|---|\n"
        )
        for t in list_themes():
            mark = "*" if t.name == active else " "
            tone = theme_kind(t)
            from synapse.ui.theme import BUILTIN_THEMES, _custom

            if t.name in _custom and t.name not in BUILTIN_THEMES:
                kind = f"custom/{tone}"
            elif t.name in _custom:
                kind = f"override/{tone}"
            else:
                kind = f"built-in/{tone}"
            md += (
                f"| {mark} | `{markdown_escape(t.name)}` "
                f"| {markdown_escape(t.label)} | {markdown_escape(kind)} |\n"
            )
        md += "\nusage: `/theme <name>`\n\nconfig: `settings.json` theme + optional `themes.json`"
        return SlashResult(handled=True, lines=plain, markdown=md)

    name = args[0].strip()
    # Optional: /theme set <name> | /theme use <name>
    if name.casefold() in {"set", "use", "switch"} and len(args) >= 2:
        name = args[1].strip()
    try:
        theme = set_theme(
            name,
            workspace=project_root,
            persist=True,
            scope="user",
            reload=False,
        )
    except KeyError as exc:
        names = ", ".join(list_theme_names())
        return SlashResult(
            handled=True,
            lines=[str(exc), f"available: {names}"],
            error=True,
        )
    except Exception as exc:  # noqa: BLE001
        return SlashResult(
            handled=True,
            lines=[f"theme switch failed: {exc}"],
            error=True,
        )

    try:
        settings.theme = theme.name
    except Exception:  # noqa: BLE001
        pass
    return SlashResult(
        handled=True,
        lines=[
            f"theme switched to {theme.name} ({theme.label})",
            "saved to ~/.coding-agent/settings.json",
        ],
        markdown=(
            "## Theme\n\n"
            f"- **name**: `{markdown_escape(theme.name)}`\n"
            f"- **label**: {markdown_escape(theme.label)}\n\n"
            "*saved to `~/.coding-agent/settings.json`*"
        ),
        settings_changed=True,
        theme_name=theme.name,
    )
