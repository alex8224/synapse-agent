"""Shared interactive slash commands for chat CLI and TUI.

Focus: session management + MCP management first.
Returns structured results so UIs only need to render/apply side effects.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from synapse.agent import build_coding_agent
from synapse.mcp_client import (
    get_active_mcp_pool,
    load_mcp_server_configs,
    load_mcp_tools,
)
from synapse.models_registry import registry_from_settings
from synapse.sessions import (
    SessionStore,
    allocate_thread_id,
    apply_binding_to_settings,
    binding_from_settings,
    format_session_table,
)
from synapse.transcript import (
    export_transcript_json,
    export_transcript_markdown,
    load_thread_messages,
)

HELP_TEXT = """slash commands:
  /help
  /thread | /id
  /clear                  (tui only)
  /exit | /quit

  completion:
    Tab / Right     accept suggestion
    Shift+Tab       previous candidate (tui)
    Ctrl+Space      list candidates (tui)
    Tab             complete (chat readline)

  session:
    /sessions | /session list [n]
    /session | /session show
    /new
    /switch <thread_id>
    /rename <title>
    /session delete <thread_id>
    /session search <query>
    /export [md|json] [path]

  mcp:
    /mcp | /mcp list
    /mcp tools
    /mcp test
    /mcp reload
    /mcp enable | /mcp disable
    /mcp config

  model:
    /model
    /model <alias|provider:model>
    /model thinking <off|low|medium|high|max>

  appearance:
    /theme
    /theme list
    /theme <name>              (persist to ~/.coding-agent/settings.json)

  safety / HITL:
    /safety                     show profile
    /safety dev-autopass|dev-approve|readonly
    /approve                    approve pending tool call(s)
    /reject [reason]            reject pending tool call(s)
"""



@dataclass
class SlashResult:
    """Outcome of a slash command."""

    handled: bool = False
    lines: list[str] = field(default_factory=list)
    error: bool = False
    # Short one-line confirmation for the bottom status bar (never transcript).
    notice: str | None = None
    exit_requested: bool = False
    clear_log: bool = False
    reload_transcript: bool = False
    thread_id: str | None = None
    agent: Any | None = None
    settings_changed: bool = False
    # UI theme switch (TUI should re-apply CSS / palette).
    theme_name: str | None = None
    # HITL: UI should resume the paused graph with this decision.
    resume_action: str | None = None  # "approve" | "reject"
    resume_message: str | None = None


def _parts(text: str) -> list[str]:
    return text.strip().split()


def _store(settings: Any) -> SessionStore:
    return SessionStore(settings.resolved_sessions_path())


def _model_name(settings: Any) -> str:
    return str(getattr(settings, "model", "") or "")


def _session_show(store: SessionStore, thread_id: str, settings: Any) -> list[str]:
    info = store.get(thread_id) or store.ensure(
        thread_id,
        model=_model_name(settings),
        active_model=getattr(settings, "active_model", None),
        thinking=binding_from_settings(settings).thinking,
    )
    bind = info.binding()
    return [
        f"current session: {info.thread_id}",
        f"  title: {info.title}",
        f"  model: {bind.display()}",
        f"  active_model: {info.active_model or '-'}",
        f"  thinking: {info.thinking or '-'}",
        f"  created: {info.created_at}",
        f"  updated: {info.updated_at}",
        f"  tags: {', '.join(info.tags) if info.tags else '-'}",
    ]


def _persist_model_binding(settings: Any, thread_id: str | None) -> None:
    try:
        store = _store(settings)
        store.save_model_binding(thread_id, binding_from_settings(settings), also_last=True)
    except Exception:  # noqa: BLE001
        pass


def _restore_thread_model(
    *,
    settings: Any,
    agent: Any,
    project_root: Path,
    thread_id: str,
) -> tuple[Any | None, list[str]]:
    """Restore model binding for a thread. Returns (new_agent|None, notes)."""
    store = _store(settings)
    binding = store.get_model_binding(thread_id)
    if not binding.has_data():
        return None, []
    changed = apply_binding_to_settings(settings, binding)
    if not changed:
        return None, [f"model binding: {binding.display()}"]
    try:
        new_agent = _rebuild_agent(
            settings,
            project_root=project_root,
            model_name=settings.active_model or settings.model,
            agent=agent,
        )
    except Exception as exc:  # noqa: BLE001
        return None, [f"restore model failed: {exc}"]
    return new_agent, [f"restored model: {binding.display()}"]


def _load_messages(agent: Any, settings: Any, thread_id: str) -> list[Any]:
    return load_thread_messages(agent=agent, settings=settings, thread_id=thread_id)


def _export_lines(
    *,
    settings: Any,
    agent: Any,
    thread_id: str,
    fmt: str,
    out_path: Path | None,
) -> SlashResult:
    store = _store(settings)
    model = _model_name(settings)
    info = store.get(thread_id) or store.ensure(thread_id, model=model)
    messages = _load_messages(agent, settings, thread_id)

    if fmt in {"json", "j"}:
        payload = export_transcript_json(
            thread_id=thread_id,
            title=info.title,
            model=info.model or model,
            messages=messages,
            meta=info.to_dict(),
        )
        text = json.dumps(payload, ensure_ascii=False, indent=2)
    else:
        text = export_transcript_markdown(
            thread_id=thread_id,
            title=info.title,
            model=info.model or model,
            messages=messages,
        )
        # Keep metadata section readable when transcript empty.
        if not messages:
            meta = store.export_markdown(thread_id) or ""
            text = meta + "\n## Transcript\n\n(no checkpoint messages found)\n"

    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        return SlashResult(
            handled=True,
            lines=[
                f"exported {fmt} -> {out_path}",
                f"messages: {len(messages)}",
            ],
        )
    return SlashResult(handled=True, lines=text.splitlines() or ["(empty export)"])


def _handle_session(
    cmd: str,
    args: list[str],
    *,
    settings: Any,
    agent: Any,
    thread_id: str,
) -> SlashResult:
    store = _store(settings)
    model = _model_name(settings)

    if cmd in {"/sessions", "/session"} and not args:
        if cmd == "/session":
            return SlashResult(handled=True, lines=_session_show(store, thread_id, settings))
        return SlashResult(
            handled=True,
            lines=[format_session_table(store.list_nonempty())],
        )

    if cmd == "/sessions" and args:
        sub = args[0].lower()
        rest = args[1:]
        return _handle_session(
            "/session",
            [sub, *rest],
            settings=settings,
            agent=agent,
            thread_id=thread_id,
        )

    if cmd == "/new":
        tid = allocate_thread_id()
        bind = binding_from_settings(settings)
        # Do not persist until the first user message (avoids empty session junk).
        store.set_last_model_binding(bind)
        return SlashResult(
            handled=True,
            lines=[
                f"new session thread_id={tid}  model={bind.display()}",
                "session metadata is saved on the first message",
            ],
            thread_id=tid,
            clear_log=True,
            reload_transcript=False,
        )

    if cmd == "/switch":
        if not args:
            return SlashResult(
                handled=True,
                lines=["usage: /switch <thread_id|title>"],
                error=True,
            )
        query = " ".join(args).strip()
        info = store.resolve_session_ref(query)
        if info is None:
            tid = args[0].strip()
            if len(args) > 1 or " " in query:
                return SlashResult(
                    handled=True,
                    lines=[
                        f"session not found: {query}",
                        "tip: /sessions — list titles; match must be unique",
                    ],
                    error=True,
                )
            store.ensure(tid, model=model)
            return SlashResult(
                handled=True,
                lines=[f"switched thread_id={tid}"],
                thread_id=tid,
                settings_changed=True,
                clear_log=True,
                reload_transcript=True,
            )
        return SlashResult(
            handled=True,
            lines=[f"switched thread_id={info.thread_id}  title={info.title}"],
            thread_id=info.thread_id,
            settings_changed=True,
            clear_log=True,
            reload_transcript=True,
        )

    if cmd == "/rename":
        if not args:
            return SlashResult(handled=True, lines=["usage: /rename <title>"], error=True)
        title = " ".join(args).strip()
        store.ensure(thread_id, model=model)
        info = store.rename(thread_id, title)
        return SlashResult(
            handled=True,
            lines=[f"renamed to: {info.title if info else title}"],
        )

    if cmd == "/export":
        fmt = "md"
        out_path: Path | None = None
        if args:
            fmt = args[0].lower()
            if len(args) >= 2:
                out_path = Path(" ".join(args[1:])).expanduser()
        return _export_lines(
            settings=settings,
            agent=agent,
            thread_id=thread_id,
            fmt=fmt,
            out_path=out_path,
        )

    if cmd != "/session":
        return SlashResult(handled=False)

    if not args:
        return SlashResult(handled=True, lines=_session_show(store, thread_id, settings))

    sub = args[0].lower()
    rest = args[1:]

    if sub in {"list", "ls"}:
        limit = 50
        if rest:
            try:
                limit = max(1, int(rest[0]))
            except ValueError:
                return SlashResult(
                    handled=True,
                    lines=["usage: /session list [n]"],
                    error=True,
                )
        return SlashResult(
            handled=True,
            lines=[format_session_table(store.list_nonempty(limit=limit))],
        )

    if sub == "prune":
        deleted = store.prune_empty(except_ids={thread_id} if thread_id else set())
        lines = [f"pruned {len(deleted)} empty session(s)"]
        lines.extend(f"  - {tid}" for tid in deleted[:20])
        if len(deleted) > 20:
            lines.append(f"  … and {len(deleted) - 20} more")
        return SlashResult(handled=True, lines=lines)

    if sub == "show":
        if rest:
            info = store.resolve_session_ref(" ".join(rest))
            if info is None:
                return SlashResult(
                    handled=True,
                    lines=[f"session not found: {' '.join(rest)}"],
                    error=True,
                )
            tid = info.thread_id
        else:
            tid = thread_id
        store.ensure(tid, model=model)
        return SlashResult(handled=True, lines=_session_show(store, tid, settings))

    if sub == "new":
        return _handle_session(
            "/new", [], settings=settings, agent=agent, thread_id=thread_id
        )

    if sub == "switch":
        return _handle_session(
            "/switch", rest, settings=settings, agent=agent, thread_id=thread_id
        )

    if sub == "rename":
        return _handle_session(
            "/rename", rest, settings=settings, agent=agent, thread_id=thread_id
        )

    if sub == "delete":
        if not rest:
            return SlashResult(
                handled=True,
                lines=["usage: /session delete <thread_id|title>"],
                error=True,
            )
        query = " ".join(rest).strip()
        info = store.resolve_session_ref(query)
        tid = info.thread_id if info is not None else rest[0]
        if tid == thread_id:
            return SlashResult(
                handled=True,
                lines=["cannot delete the active session; /switch first"],
                error=True,
            )
        ok = store.delete(tid)
        if ok:
            label = info.title if info is not None else tid
            return SlashResult(
                handled=True,
                lines=[f"deleted session metadata: {tid}  ({label})"],
            )
        return SlashResult(handled=True, lines=[f"session not found: {query}"], error=True)

    if sub == "search":
        if not rest:
            return SlashResult(
                handled=True,
                lines=["usage: /session search <query>"],
                error=True,
            )
        q = " ".join(rest)
        return SlashResult(
            handled=True,
            lines=[format_session_table(store.search(q))],
        )

    if sub == "export":
        return _handle_session(
            "/export", rest, settings=settings, agent=agent, thread_id=thread_id
        )

    return SlashResult(
        handled=True,
        lines=[
            "usage: /session [list|show|new|switch|rename|delete|search|export]",
            "also: /sessions /new /switch /rename /export",
        ],
        error=True,
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
        lines.append(
            f"- {s.name}: transport={s.transport} enabled={s.enabled} {dest}"
        )
    loaded = getattr(build_coding_agent, "last_mcp_servers", []) or []
    warnings = getattr(build_coding_agent, "last_mcp_warnings", []) or []
    tool_names = getattr(build_coding_agent, "last_mcp_tool_names", []) or []
    lines.append(f"loaded at agent build: {', '.join(loaded) or '(none)'}")
    lines.append(f"tools bound: {len(tool_names)}")
    for w in warnings:
        lines.append(f"warn: {w}")
    return lines


def _rebuild_agent(
    settings: Any,
    *,
    project_root: Path,
    model_name: str | None,
    agent: Any,
    load_mcp: bool | None = None,
) -> Any:
    checkpointer = getattr(agent, "_coding_checkpointer", None)
    # Reuse live model only when not switching profiles.
    reuse_model = model_name is None
    model = getattr(agent, "_coding_model", None) if reuse_model else None
    registry = getattr(agent, "_coding_model_registry", None) if reuse_model else None
    mcp_tools: list[Any] | None = None
    if load_mcp is not None:
        # Explicit caller intent (/mcp reload, /mcp disable, ...).
        want_mcp = bool(load_mcp)
    elif not bool(getattr(settings, "enable_mcp", True)):
        want_mcp = False
    else:
        # Prefer the live pool when it already has tools, regardless of the
        # old agent's attached flag. This covers:
        #  - normal attached agent (flag True + pool alive) → reuse
        #  - startup race: pool connected but agent not yet swapped → reuse
        #  - after /mcp reload that created a new pool → reuse
        pool = get_active_mcp_pool()
        pool_tools = list(getattr(pool, "tools", None) or []) if pool is not None else []
        if pool is not None:
            mcp_tools = pool_tools
            want_mcp = False
        elif bool(getattr(agent, "_coding_mcp_attached", False)):
            # Was attached but pool vanished — reconnect once.
            want_mcp = True
        else:
            # MCP was deferred at startup and no pool yet: stay deferred.
            want_mcp = False
    return build_coding_agent(
        settings,
        project_root=project_root,
        model_name=model_name,
        checkpointer=checkpointer,
        model=model,
        model_registry=registry,
        load_mcp=want_mcp,
        mcp_tools=mcp_tools,
    )


def _apply_thinking_inplace(settings: Any, agent: Any, model_name: str) -> bool:
    """Update thinking params on the live model without rebuilding the graph.

    Constructs a fresh (cheap, no network) chat model with the new settings and
    copies thinking-related attributes onto the live instance. Returns False
    when in-place update is not possible so callers can fall back to rebuild.
    """
    from synapse.models_registry import build_model_from_settings

    live = getattr(agent, "_coding_model", None)
    if live is None:
        return False
    try:
        _, fresh = build_model_from_settings(settings, model_name=model_name)
    except Exception:  # noqa: BLE001
        return False
    if type(fresh) is not type(live):
        return False
    copied = False
    for attr in ("reasoning_effort", "extra_body", "thinking", "model_kwargs"):
        if not (hasattr(fresh, attr) and hasattr(live, attr)):
            continue
        try:
            setattr(live, attr, getattr(fresh, attr))
            copied = True
        except Exception:  # noqa: BLE001
            return False
    return copied


def _handle_mcp(
    args: list[str],
    *,
    settings: Any,
    agent: Any,
    project_root: Path,
    model_name: str | None,
) -> SlashResult:
    sub = (args[0].lower() if args else "list").strip()

    if sub in {"list", "ls", "status"}:
        return SlashResult(handled=True, lines=_mcp_list_lines(settings))

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
        return SlashResult(handled=True, lines=lines)

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
            return SlashResult(handled=True, lines=lines)
        lines = [f"mcp tools ({len(names)}):"]
        for n in names:
            lines.append(f"- {n}")
        return SlashResult(handled=True, lines=lines)

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
        return SlashResult(handled=True, lines=lines)

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
                }
                for s in servers
            ]
        }
        settings.mcp_servers_json = json.dumps(raw)
        try:
            new_agent = _rebuild_agent(
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
        notice = (
            f"mcp '{target}' {'enabled' if changed.enabled else 'disabled'}"
            f" · tools={len(tools)}"
        )
        lines = [
            f"mcp server '{target}' {'enabled' if changed.enabled else 'disabled'}; agent rebuilt",
            f"loaded servers: {', '.join(loaded) or '(none)'}",
            f"tools bound: {len(tools)}",
        ]
        for w in warnings_list:
            lines.append(f"warn: {w}")
        return SlashResult(
            handled=True,
            lines=lines,
            notice=notice,
            agent=new_agent,
            settings_changed=True,
        )

    if sub in {"enable", "on"}:
        settings.enable_mcp = True
        return SlashResult(
            handled=True,
            lines=["mcp enabled (run /mcp reload to rebuild agent)"],
            notice="mcp enabled · reload pending",
            settings_changed=True,
        )

    if sub in {"disable", "off"}:
        settings.enable_mcp = False
        try:
            new_agent = _rebuild_agent(
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
            notice="mcp disabled · tools=0",
            agent=new_agent,
            settings_changed=True,
        )

    if sub == "reload":
        try:
            new_agent = _rebuild_agent(
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
        notice = f"mcp reloaded · servers={len(loaded)} tools={len(tools)}"
        if not enabled_flag:
            notice = "mcp reloaded · disabled"
        lines = [
            f"agent rebuilt; mcp enabled={getattr(settings, 'enable_mcp', True)}",
            f"loaded servers: {', '.join(loaded) or '(none)'}",
            f"tools bound: {len(tools)}",
        ]
        for w in warnings:
            lines.append(f"warn: {w}")
        return SlashResult(handled=True, lines=lines, notice=notice, agent=new_agent)

    return SlashResult(
        handled=True,
        lines=["usage: /mcp [list|tools|test|reload|enable|disable|config]"],
        error=True,
    )


def _handle_model(
    args: list[str],
    *,
    settings: Any,
    agent: Any,
    project_root: Path,
    thread_id: str | None = None,
) -> SlashResult:
    from synapse.models_registry import (
        apply_thinking_to_settings,
        format_model_status,
        is_thinking_token,
        settings_thinking_label,
    )

    reg = registry_from_settings(settings)
    cfg_path = getattr(settings, "models_config_path", None)
    active = (
        getattr(agent, "_coding_model_profile", None)
        or settings.active_model
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
            label = apply_thinking_to_settings(
                settings, args[1], allowed=allowed
            )
        except ValueError as exc:
            return SlashResult(handled=True, lines=[str(exc)], error=True)
        model_name = settings.active_model or reg.default
        new_agent = None
        note = ""
        if _apply_thinking_inplace(settings, agent, model_name):
            note = " (live, no rebuild)"
        else:
            try:
                new_agent = _rebuild_agent(
                    settings,
                    project_root=project_root,
                    model_name=model_name,
                    agent=agent,
                )
            except Exception as exc:  # noqa: BLE001
                return SlashResult(
                    handled=True,
                    lines=[f"thinking update failed: {exc}"],
                    error=True,
                )
        _persist_model_binding(settings, thread_id)
        return SlashResult(
            handled=True,
            lines=[f"thinking set to {label}{note}  ({format_model_status(settings)})"],
            agent=new_agent,
            settings_changed=True,
        )

    target = args[0].strip()
    try:
        profile = reg.get(target)
    except KeyError as exc:
        return SlashResult(handled=True, lines=[str(exc)], error=True)

    from synapse.models_registry import apply_profile_to_settings

    apply_profile_to_settings(settings, profile, seed_thinking=True)

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

    if think_raw is not None:
        try:
            apply_thinking_to_settings(
                settings,
                think_raw,
                allowed=reg.allowed_thinking_levels(profile.name),
            )
        except ValueError as exc:
            return SlashResult(handled=True, lines=[str(exc)], error=True)

    try:
        new_agent = _rebuild_agent(
            settings,
            project_root=project_root,
            model_name=profile.name,
            agent=agent,
        )
    except Exception as exc:  # noqa: BLE001
        return SlashResult(
            handled=True,
            lines=[f"model switch failed: {exc}"],
            error=True,
        )
    _persist_model_binding(settings, thread_id)
    return SlashResult(
        handled=True,
        lines=[
            f"model switched to {profile.name}  ({format_model_status(settings)})"
        ],
        agent=new_agent,
        settings_changed=True,
    )



def _handle_theme(
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
        return SlashResult(handled=True, lines=format_theme_list_lines(active=active))

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
        settings_changed=True,
        theme_name=theme.name,
    )


def handle_slash(
    text: str,
    *,
    settings: Any,
    agent: Any,
    thread_id: str,
    project_root: Path | None = None,
) -> SlashResult:
    """Parse and handle a slash command. Non-commands return handled=False."""
    raw = (text or "").strip()
    if not raw.startswith("/") and raw not in {":q"}:
        return SlashResult(handled=False)

    root = Path(project_root or Path.cwd()).resolve()
    model_name = getattr(settings, "active_model", None)

    if raw in {"/exit", "/quit", ":q"}:
        return SlashResult(
            handled=True,
            lines=[f"bye. thread_id={thread_id}"],
            exit_requested=True,
        )
    if raw in {"/thread", "/id"}:
        return SlashResult(handled=True, lines=[f"thread_id={thread_id}"])
    if raw == "/clear":
        return SlashResult(handled=True, clear_log=True, lines=["log cleared"])
    if raw in {"/help", "/?"}:
        return SlashResult(handled=True, lines=HELP_TEXT.splitlines())

    parts = _parts(raw)
    cmd = parts[0].lower()
    args = parts[1:]

    if cmd in {
        "/sessions",
        "/session",
        "/new",
        "/switch",
        "/rename",
        "/export",
    }:
        result = _handle_session(
            cmd, args, settings=settings, agent=agent, thread_id=thread_id
        )
        # When switching sessions, restore that session's model binding.
        if (
            result.handled
            and not result.error
            and result.thread_id
            and result.thread_id != thread_id
            and cmd in {"/switch", "/session"}
        ):
            new_agent, notes = _restore_thread_model(
                settings=settings,
                agent=agent,
                project_root=root,
                thread_id=result.thread_id,
            )
            if notes:
                result.lines = [*result.lines, *notes]
            if new_agent is not None:
                result.agent = new_agent
                result.settings_changed = True
        return result

    if cmd == "/mcp":
        return _handle_mcp(
            args,
            settings=settings,
            agent=agent,
            project_root=root,
            model_name=model_name,
        )

    if cmd == "/model":
        return _handle_model(
            args,
            settings=settings,
            agent=agent,
            project_root=root,
            thread_id=thread_id,
        )

    if cmd == "/theme":
        return _handle_theme(args, settings=settings, project_root=root)

    if cmd == "/compact":
        from synapse.context_compact import force_compact_via_agent

        ok, lines = force_compact_via_agent(agent, thread_id=thread_id)
        return SlashResult(handled=True, lines=lines, error=not ok)

    if cmd == "/context":
        from synapse.context_compact import context_status_lines

        return SlashResult(handled=True, lines=context_status_lines(agent, thread_id))

    if cmd == "/safety":
        from synapse.safety import (
            apply_safety_to_settings,
            format_safety_status,
            get_safety_profile,
        )

        if not args:
            return SlashResult(handled=True, lines=format_safety_status(settings))
        profile = get_safety_profile(args[0])
        notes = apply_safety_to_settings(settings, profile)
        try:
            new_agent = _rebuild_agent(
                settings,
                project_root=root,
                model_name=model_name,
                agent=agent,
            )
        except Exception as exc:  # noqa: BLE001
            return SlashResult(
                handled=True,
                lines=[*notes, f"rebuild failed: {exc}"],
                error=True,
                settings_changed=True,
            )
        return SlashResult(
            handled=True,
            lines=[*notes, "agent rebuilt"],
            agent=new_agent,
            settings_changed=True,
        )

    if cmd == "/approve":
        return SlashResult(
            handled=True,
            lines=["resume: approve pending tool call(s)"],
            resume_action="approve",
        )

    if cmd == "/reject":
        reason = " ".join(args).strip() or None
        return SlashResult(
            handled=True,
            lines=["resume: reject pending tool call(s)"],
            resume_action="reject",
            resume_message=reason,
        )

    if cmd == "/skills":
        from synapse.skills_catalog import (
            discover_skills,
            format_skills_lines,
            skills_paths_from_settings,
        )

        paths = skills_paths_from_settings(settings, root)
        return SlashResult(handled=True, lines=format_skills_lines(discover_skills(paths)))

    if cmd == "/memory":
        from synapse.skills_catalog import (
            format_memory_lines,
            list_memory_files,
            memory_paths_from_settings,
        )

        paths = memory_paths_from_settings(settings, root)
        return SlashResult(handled=True, lines=format_memory_lines(list_memory_files(paths)))

    if cmd in {"/subagents", "/subagent"}:
        from synapse.subagents import build_default_subagents, format_subagents_lines

        specs = getattr(agent, "_coding_subagents", None)
        if specs is None:
            specs = build_default_subagents(
                enabled=getattr(settings, "enable_subagents", True),
                isolate_tools=True,
            )
        return SlashResult(handled=True, lines=format_subagents_lines(specs))

    if raw.startswith("/"):
        return SlashResult(
            handled=True,
            lines=[f"unknown command: {cmd}", "type /help for commands"],
            error=True,
        )
    return SlashResult(handled=False)
