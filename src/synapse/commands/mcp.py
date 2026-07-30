"""MCP slash-command handler and presentation helpers."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from synapse.app.agent import build_coding_agent
from synapse.commands.helpers import markdown_escape
from synapse.commands.result import SlashResult
from synapse.integrations.mcp_client import (
    get_active_mcp_pool,
    load_mcp_server_configs,
    load_mcp_tools,
)


def _mcp_list_lines(settings: Any) -> list[str]:
    servers = load_mcp_server_configs(
        path=getattr(settings, "mcp_config_path", None),
        json_blob=getattr(settings, "mcp_servers_json", None),
    )
    enable = bool(getattr(settings, "enable_mcp", True))
    lines = [f"mcp enabled={enable}"]
    pool = get_active_mcp_pool()
    if pool is not None:
        lines.append(f"live pool servers: {', '.join(pool.server_names) or '(none)'}")
    if not servers:
        lines.append("no MCP servers configured")
        path = getattr(settings, "mcp_config_path", None)
        if path:
            lines.append(f"config path: {path}")
        return lines
    for s in servers:
        if s.transport == "stdio":
            dest = f"cmd={s.command!r} args={s.args!r}"
        else:
            dest = f"url={s.url!r}"
        lines.append(f"- {s.name}: transport={s.transport} enabled={s.enabled} {dest}")
    loaded = getattr(build_coding_agent, "last_mcp_servers", []) or []
    warnings = getattr(build_coding_agent, "last_mcp_warnings", []) or []
    tool_names = getattr(build_coding_agent, "last_mcp_tool_names", []) or []
    lines.append(f"loaded at agent build: {', '.join(loaded) or '(none)'}")
    lines.append(f"tools bound: {len(tool_names)}")
    for w in warnings:
        lines.append(f"warn: {w}")
    return lines


def _md_mcp_list(settings: Any) -> str:
    """Format MCP server config as a Markdown table."""
    servers = load_mcp_server_configs(
        path=getattr(settings, "mcp_config_path", None),
        json_blob=getattr(settings, "mcp_servers_json", None),
    )
    enable = bool(getattr(settings, "enable_mcp", True))
    lines = [f"## MCP Servers  (`mcp enabled={enable}`)", ""]
    pool = get_active_mcp_pool()
    if pool is not None:
        lines.append(f"**live pool**: {', '.join(pool.server_names) or '(none)'}")
        lines.append("")
    if not servers:
        lines.append("*No MCP servers configured*")
        path = getattr(settings, "mcp_config_path", None)
        if path:
            lines.append(f"\nconfig path: `{path}`")
        return "\n".join(lines)
    lines.append("| Name | Transport | Enabled | Endpoint |")
    lines.append("|---|---|---|---|")
    for s in servers:
        if s.transport == "stdio":
            dest = f"`{s.command or '-'}`"
            if s.args:
                dest += " " + " ".join(str(a) for a in s.args)
        else:
            dest = s.url or "-"
        lines.append(
            f"| {markdown_escape(s.name)} | {markdown_escape(s.transport)} "
            f"| {'yes' if s.enabled else 'no'} | {markdown_escape(dest)} |"
        )
    loaded = getattr(build_coding_agent, "last_mcp_servers", []) or []
    tool_names = getattr(build_coding_agent, "last_mcp_tool_names", []) or []
    warnings_list = getattr(build_coding_agent, "last_mcp_warnings", []) or []
    lines.append("")
    lines.append(f"**loaded at agent build**: {', '.join(loaded) or '(none)'}")
    lines.append(f"**tools bound**: {len(tool_names)}")
    for w in warnings_list:
        lines.append(f"- warn: {w}")
    return "\n".join(lines)


def _md_tool_list(names: list[str], warnings: list[str] | None = None) -> str:
    """Format MCP tool names as a Markdown list."""
    lines = [f"## MCP Tools ({len(names)})", ""]
    if not names:
        lines.append("*(no tools discovered)*")
    else:
        for n in sorted(names):
            lines.append(f"- `{markdown_escape(n)}`")
    for w in warnings or []:
        lines.append(f"\nwarn: {w}")
    return "\n".join(lines)


def handle_mcp(
    args: list[str],
    *,
    settings: Any,
    agent: Any,
    project_root: Path,
    model_name: str | None,
    rebuild_agent: Callable[..., Any],
) -> SlashResult:
    sub = (args[0].lower() if args else "list").strip()

    if sub in {"list", "ls", "status"}:
        lines = _mcp_list_lines(settings)
        return SlashResult(handled=True, lines=lines, markdown=_md_mcp_list(settings))

    if sub == "config":
        path = getattr(settings, "mcp_config_path", None)
        blob = getattr(settings, "mcp_servers_json", None)
        lines = [
            f"enable_mcp={getattr(settings, 'enable_mcp', True)}",
            f"mcp_config_path={path!s}",
            f"mcp_servers_json set={bool(blob and str(blob).strip())}",
            "transports: stdio | sse | streamable_http(http)",
        ]
        if path and Path(path).is_file():
            lines.append(f"config file exists: {path}")
        elif path:
            lines.append(f"config file missing: {path}")
        file_status = ""
        if path:
            file_status = "exists" if Path(path).is_file() else "missing"
        return SlashResult(
            handled=True,
            lines=lines,
            markdown=(
                "## MCP Config\n\n"
                "| Setting | Value |\n|---|---|\n"
                f"| enable_mcp | `{getattr(settings, 'enable_mcp', True)}` |\n"
                f"| config_path | `{path}` |\n"
                f"| config_json_set | `{bool(blob and str(blob).strip())}` |\n"
                f"| transports | stdio, sse, streamable_http |\n"
                + (f"| file_status | {file_status} |\n" if path else "")
            ),
        )

    if sub == "tools":
        names = getattr(build_coding_agent, "last_mcp_tool_names", []) or []
        pool = get_active_mcp_pool()
        if pool is not None and pool.tool_names:
            names = list(pool.tool_names)
        if not names:
            # Fall back to a probe without replacing active pool permanently if empty.
            servers = load_mcp_server_configs(
                path=getattr(settings, "mcp_config_path", None),
                json_blob=getattr(settings, "mcp_servers_json", None),
            )
            if not servers:
                return SlashResult(handled=True, lines=["no MCP servers configured"])
            if not getattr(settings, "enable_mcp", True):
                return SlashResult(
                    handled=True,
                    lines=["mcp disabled; use /mcp enable then /mcp reload"],
                    error=True,
                )
            result = load_mcp_tools(servers, enabled=True)
            names = list(result.tool_names or [getattr(t, "name", str(t)) for t in result.tools])
            lines = [f"mcp tools ({len(names)}):"]
            if not names:
                lines.append("(no tools discovered)")
            for n in names:
                lines.append(f"- {n}")
            for w in result.warnings:
                lines.append(f"warn: {w}")
            md = _md_tool_list(names, result.warnings)
            return SlashResult(handled=True, lines=lines, markdown=md)
        lines = [f"mcp tools ({len(names)}):"]
        for n in names:
            lines.append(f"- {n}")
        return SlashResult(handled=True, lines=lines, markdown=_md_tool_list(names, []))

    if sub == "test":
        servers = load_mcp_server_configs(
            path=getattr(settings, "mcp_config_path", None),
            json_blob=getattr(settings, "mcp_servers_json", None),
        )
        if not servers:
            return SlashResult(handled=True, lines=["no MCP servers configured"])
        # Group by transport for clearer diagnostics.
        by_transport: dict[str, list[str]] = {}
        for s in servers:
            by_transport.setdefault(s.transport, []).append(s.name)
        result = load_mcp_tools(servers, enabled=True)
        lines = [
            f"servers ok: {', '.join(result.servers) or '-'}",
            f"tools: {len(result.tools)}",
            "configured transports:",
        ]
        for transport, names in sorted(by_transport.items()):
            lines.append(f"  {transport}: {', '.join(names)}")
        for tool in result.tools[:30]:
            lines.append(f"- {getattr(tool, 'name', tool)}")
        if len(result.tools) > 30:
            lines.append(f"... and {len(result.tools) - 30} more")
        for w in result.warnings:
            lines.append(f"warn: {w}")
        # Build markdown
        md_lines = ["## MCP Test Results", ""]
        md_lines.append(f"- **servers ok**: {', '.join(result.servers) or '-'}")
        md_lines.append(f"- **tools discovered**: {len(result.tools)}")
        md_lines.append("")
        md_lines.append("| Transport | Servers |")
        md_lines.append("|---|---|")
        for transport, names in sorted(by_transport.items()):
            md_lines.append(
                f"| {markdown_escape(transport)} | {markdown_escape(', '.join(names))} |"
            )
        if result.tools:
            md_lines.append("")
            md_lines.append(f"### Tools ({len(result.tools)})")
            for tool in result.tools[:30]:
                md_lines.append(f"- `{markdown_escape(getattr(tool, 'name', str(tool)))}`")
            if len(result.tools) > 30:
                md_lines.append(f"- *鈥?and {len(result.tools) - 30} more*")
        for w in result.warnings:
            md_lines.append(f"\nwarn: {w}")
        return SlashResult(handled=True, lines=lines, markdown="\n".join(md_lines))

    if sub == "toggle":
        if len(args) < 2:
            return SlashResult(
                handled=True,
                lines=["usage: /mcp toggle <server_name>"],
                error=True,
            )
        target = args[1].strip()
        servers = load_mcp_server_configs(
            path=getattr(settings, "mcp_config_path", None),
            json_blob=getattr(settings, "mcp_servers_json", None),
        )
        changed = None
        for s in servers:
            if s.name == target:
                s.enabled = not s.enabled
                changed = s
                break
        if changed is None:
            return SlashResult(
                handled=True,
                lines=[f"mcp server not found: {target}"],
                error=True,
            )
        # Serialize modified configs back so reload picks up the toggled state.
        raw = {
            "servers": [
                {
                    "name": s.name,
                    "transport": s.transport,
                    "command": s.command,
                    "args": s.args,
                    "env": s.env,
                    "url": s.url,
                    "headers": s.headers,
                    "enabled": s.enabled,
                    "tool_prefix": s.tool_prefix,
                    "include_tools": s.include_tools,
                    "exclude_tools": s.exclude_tools,
                }
                for s in servers
            ]
        }
        settings.mcp_servers_json = json.dumps(raw)
        try:
            new_agent = rebuild_agent(
                settings,
                project_root=project_root,
                model_name=model_name,
                agent=agent,
                load_mcp=True,
            )
        except Exception as exc:  # noqa: BLE001
            return SlashResult(
                handled=True,
                lines=[f"mcp toggle reload failed: {exc}"],
                error=True,
            )
        loaded = getattr(build_coding_agent, "last_mcp_servers", []) or []
        warnings_list = getattr(build_coding_agent, "last_mcp_warnings", []) or []
        tools = getattr(build_coding_agent, "last_mcp_tool_names", []) or []
        notice = f"mcp '{target}' {'enabled' if changed.enabled else 'disabled'} tools={len(tools)}"
        lines = [
            f"mcp server '{target}' {'enabled' if changed.enabled else 'disabled'}; agent rebuilt",
            f"loaded servers: {', '.join(loaded) or '(none)'}",
            f"tools bound: {len(tools)}",
        ]
        for w in warnings_list:
            lines.append(f"warn: {w}")
        md_lines = [
            "## MCP Toggle",
            "",
            f"**{target}**: {'enabled' if changed.enabled else 'disabled'}",
            "",
            f"- loaded servers: {', '.join(loaded) or '(none)'}",
            f"- tools bound: {len(tools)}",
        ]
        for w in warnings_list:
            md_lines.append(f"- warn: {w}")
        return SlashResult(
            handled=True,
            lines=lines,
            notice=notice,
            markdown="\n".join(md_lines),
            agent=new_agent,
            settings_changed=True,
        )

    if sub in {"enable", "on"}:
        settings.enable_mcp = True
        return SlashResult(
            handled=True,
            lines=["mcp enabled (run /mcp reload to rebuild agent)"],
            notice="mcp enabled reload pending",
            settings_changed=True,
        )

    if sub in {"disable", "off"}:
        settings.enable_mcp = False
        try:
            new_agent = rebuild_agent(
                settings,
                project_root=project_root,
                model_name=model_name,
                agent=agent,
                load_mcp=False,
            )
        except Exception as exc:  # noqa: BLE001
            return SlashResult(
                handled=True,
                lines=[f"mcp disable rebuild failed: {exc}"],
                error=True,
            )
        return SlashResult(
            handled=True,
            lines=["mcp disabled; agent rebuilt without MCP tools"],
            notice="mcp disabled tools=0",
            agent=new_agent,
            settings_changed=True,
        )

    if sub == "reload":
        try:
            new_agent = rebuild_agent(
                settings,
                project_root=project_root,
                model_name=model_name,
                agent=agent,
                load_mcp=True,
            )
        except Exception as exc:  # noqa: BLE001
            return SlashResult(
                handled=True,
                lines=[f"mcp reload failed: {exc}"],
                error=True,
            )
        loaded = getattr(build_coding_agent, "last_mcp_servers", []) or []
        warnings = getattr(build_coding_agent, "last_mcp_warnings", []) or []
        tools = getattr(build_coding_agent, "last_mcp_tool_names", []) or []
        enabled_flag = bool(getattr(settings, "enable_mcp", True))
        notice = f"mcp reloaded servers={len(loaded)} tools={len(tools)}"
        if not enabled_flag:
            notice = "mcp reloaded disabled"
        lines = [
            f"agent rebuilt; mcp enabled={getattr(settings, 'enable_mcp', True)}",
            f"loaded servers: {', '.join(loaded) or '(none)'}",
            f"tools bound: {len(tools)}",
        ]
        for w in warnings:
            lines.append(f"warn: {w}")
        md_lines = [
            "## MCP Reloaded",
            "",
            f"- **enabled**: `{enabled_flag}`",
            f"- **servers**: {', '.join(loaded) or '(none)'}",
            f"- **tools**: {len(tools)}",
        ]
        for w in warnings:
            md_lines.append(f"- warn: {w}")
        return SlashResult(
            handled=True,
            lines=lines,
            notice=notice,
            markdown="\n".join(md_lines),
            agent=new_agent,
        )

    return SlashResult(
        handled=True,
        lines=["usage: /mcp [list|tools|test|reload|enable|disable|config]"],
        error=True,
    )
