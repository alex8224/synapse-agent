"""Shared interactive slash commands for chat CLI and TUI.

Focus: session management + MCP management first.
Returns structured results so UIs only need to render/apply side effects.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from synapse.app.agent import build_coding_agent
from synapse.commands.compression import handle_compression
from synapse.commands.helpers import markdown_escape, parts
from synapse.commands.mcp import handle_mcp
from synapse.commands.model import handle_model
from synapse.commands.result import SlashResult
from synapse.commands.sessions import handle_session
from synapse.commands.theme import handle_theme
from synapse.integrations.mcp_client import (
    get_active_mcp_pool,
)
from synapse.sessions.store import (
    SessionStore,
    apply_binding_to_settings,
    binding_from_settings,
)

HELP_TEXT = """## Slash Commands

### General
| Command | Description |
|---|---|
| `/help`, `/?` | Show this help |
| `/thread`, `/id` | Show current thread ID |
| `/clear` | Clear transcript (TUI only) |
| `/exit`, `/quit` | Exit |

### Completion
| Key | Action |
|---|---|
| Tab / Right | Accept suggestion |
| Shift+Tab | Previous candidate (TUI) |
| Ctrl+Space | List candidates (TUI) |

### Session
| Command | Description |
|---|---|
| `/sessions`, `/session list [n]` | List recent sessions |
| `/session`, `/session show` | Show current session |
| `/new` | Create new session |
| `/switch <id>` | Switch to session |
| `/rename <title>` | Rename current session |
| `/session delete <id>` | Delete session metadata |
| `/session search <query>` | Search sessions |
| `/session prune` | Remove empty sessions |
| `/export [md\\|json] [path]` | Export transcript to file |
| `/codex import [id]` | Import Codex session (TUI) |

### Goals (long-running tasks)
| Command | Description |
|---|---|
| `/goal` | Show current long-running goal |
| `/goal <objective>` | Set a new goal (auto-continues across turns) |
| `/goal edit <objective>` | Edit the current goal |
| `/goal pause` / `/goal resume` | Pause / resume the goal |
| `/goal clear` | Clear the goal |
| `gooooal` | Alias for `/goal` |

### MCP
| Command | Description |
|---|---|
| `/mcp`, `/mcp list` | List MCP servers |
| `/mcp tools` | List MCP tools |
| `/mcp test` | Test MCP connectivity |
| `/mcp reload` | Reload MCP servers |
| `/mcp enable`, `/mcp disable` | Toggle MCP |
| `/mcp config` | Show MCP config |

### Model
| Command | Description |
|---|---|
| `/model` | Open model picker (TUI) |
| `/model <alias\\|provider:model>` | Switch model |
| `/model thinking <level>` | Set thinking level |
| `/fast [on|off|status]` | Toggle Codex Fast tier (service_tier=priority, OAuth profiles) |

### Appearance
| Command | Description |
|---|---|
| `/theme` | Open theme picker (TUI) |
| `/theme list` | List themes |
| `/theme <name>` | Apply theme |

### Transcript
| Command | Description |
|---|---|
| `/select` | Open plain-text view for mouse selection & copy |

### Safety / HITL
| Command | Description |
|---|---|
| `/safety` | Show safety profile |
| `/safety <profile>` | Switch profile |
| `/approve` | Approve pending tools |
| `/reject [reason]` | Reject pending tools |

### Diagnostics
| Command | Description |
|---|---|
| `/context` | Context usage stats |
| `/compact` | Force context compact |
| `/compression [session]` | Compression diagnostics summary |
| `/compression profile [session]` | Request content breakdown and opportunity ranking |
| `/compression export [session] [json|csv] [path]` | Export complete compression diagnostics |
| `/compression events [session] [limit]` | Recent compression decisions |
| `/compression requests [session] [limit]` | Model request before/after ledger |
| `/compression request <request_id> [session]` | One model request accounting event |
| `/compression skipped [session] [limit]` | Outputs skipped by policy or threshold |
| `/compression fallback [session] [limit]` | Compression attempts that reverted |
| `/compression tool <tool_call_id> [session] [limit]` | Decisions for one tool call |
| `/tool-output ...` | Alias for `/compression ...` |
| `/skills` | List skills |
| `/memory` | List memory files |
| `/subagents` | List sub-agents |
| `/image [auto\|tgp\|sixel\|halfcell\|unicode]` | Image renderer diagnostic / switch |
"""



def _is_goal_alias(cmd: str) -> bool:
    """识别 ``gooooal``（g + o* + al）别名。"""
    name = cmd.casefold()
    if not (name.startswith("/g") and name.endswith("al")):
        return False
    middle = name[2:-2]
    return bool(middle and set(middle) == {"o"})


def _store(settings: Any) -> SessionStore:
    return SessionStore(settings.resolved_sessions_path())


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


def _rebuild_agent(
    settings: Any,
    *,
    project_root: Path,
    model_name: str | None,
    agent: Any,
    load_mcp: bool | None = None,
    defer_mcp_reconnect: bool = False,
) -> Any:
    checkpointer = getattr(agent, "_coding_checkpointer", None)
    steer_queue = getattr(agent, "_coding_steer_queue", None)
    # Reuse live model only when not switching profiles.
    reuse_model = model_name is None
    model = getattr(agent, "_coding_model", None) if reuse_model else None
    registry = getattr(agent, "_coding_model_registry", None) if reuse_model else None
    model_cache = getattr(agent, "_coding_model_cache", None)
    prompt_cache_key = getattr(agent, "_coding_prompt_cache_key", None)
    mcp_tools: list[Any] | None = None
    if load_mcp is not None:
        # Explicit caller intent (/mcp reload, /mcp disable, ...).
        want_mcp = bool(load_mcp)
    elif not bool(getattr(settings, "enable_mcp", True)):
        want_mcp = False
    else:
        # Prefer the live pool when it already has tools, regardless of the
        # old agent's attached flag. This covers:
        #  - normal attached agent (flag True + pool alive) 鈫?reuse
        #  - startup race: pool connected but agent not yet swapped 鈫?reuse
        #  - after /mcp reload that created a new pool 鈫?reuse
        pool = get_active_mcp_pool()
        pool_tools = list(getattr(pool, "tools", None) or []) if pool is not None else []
        if pool is not None:
            mcp_tools = pool_tools
            want_mcp = False
        elif bool(getattr(agent, "_coding_mcp_attached", False)):
            # Model switching may defer this network I/O to a TUI worker. Other
            # rebuild callers keep the historical synchronous reconnect behavior.
            want_mcp = not defer_mcp_reconnect
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
        model_cache=model_cache,
        load_mcp=want_mcp,
        mcp_tools=mcp_tools,
        steer_queue=steer_queue,
        prompt_cache_key=prompt_cache_key,
    )


def _mcp_attach_pending(settings: Any) -> bool:
    return bool(getattr(settings, "enable_mcp", True) and get_active_mcp_pool() is None)


def _fast_mode_active(settings: Any) -> bool:
    return bool(getattr(settings, "openai_fast_mode", False))


def handle_fast(args: list[str], *, settings: Any) -> SlashResult:
    """Handle ``/fast``: show or toggle the Codex Fast tier (service_tier=priority)."""
    arg = (args[0] if args else "").strip().lower()
    current = _fast_mode_active(settings)
    if arg in {"", "status"}:
        state = "on" if current else "off"
        note = "" if current else " (use /fast on)"
        return SlashResult(
            handled=True,
            lines=[f"fast mode: {state}{note} — Codex Fast tier (service_tier=priority)"],
            markdown=f"## Fast Mode\n\nStatus: **{state}**\n\n"
            "Applies to Codex OAuth profiles only (OpenAI Responses API). "
            "Toggle with `/fast on` / `/fast off`.",
        )
    if arg in {"on", "enable"}:
        if current:
            return SlashResult(handled=True, lines=["fast mode: already on"])
        settings.openai_fast_mode = True
        return SlashResult(
            handled=True,
            lines=["fast mode: on — requests use service_tier=priority"],
            notice="fast on",
            settings_changed=True,
        )
    if arg in {"off", "disable"}:
        if not current:
            return SlashResult(handled=True, lines=["fast mode: already off"])
        settings.openai_fast_mode = False
        return SlashResult(
            handled=True,
            lines=["fast mode: off"],
            notice="fast off",
            settings_changed=True,
        )
    return SlashResult(
        handled=True,
        lines=[f"unknown /fast argument: {arg}", "usage: /fast [on|off|status]"],
        error=True,
    )


def _apply_thinking_inplace(settings: Any, agent: Any, model_name: str) -> bool:
    """Update thinking params on the live model without rebuilding the graph.

    Returns False so callers fall back to a full rebuild. In-place mutation
    of a shared ChatModel instance is unsafe in a multi-session process: the
    process-wide ``model_cache`` hands the same client to every agent with an
    identical configuration key, so mutating ``reasoning_effort`` /
    ``extra_body`` / ``thinking`` here would silently change another session's
    effective model settings. A rebuild builds an independent client keyed by
    the new thinking level.
    """
    del settings, agent, model_name
    return False


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
        return SlashResult(
            handled=True,
            lines=HELP_TEXT.splitlines(),
            markdown=HELP_TEXT,
        )

    command_parts = parts(raw)
    cmd = command_parts[0].lower()
    args = command_parts[1:]

    if cmd in {
        "/sessions",
        "/session",
        "/new",
        "/switch",
        "/rename",
        "/export",
    }:
        result = handle_session(cmd, args, settings=settings, agent=agent, thread_id=thread_id)
        # Persist the outgoing session's model binding before leaving, so its
        # model cannot be silently overwritten by another session's choice
        # (settings.active_model is process-global).
        if (
            result.handled
            and not result.error
            and result.thread_id
            and result.thread_id != thread_id
            and cmd in {"/switch", "/session", "/new"}
        ):
            _persist_model_binding(settings, thread_id)
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
        return handle_mcp(
            args,
            settings=settings,
            agent=agent,
            project_root=root,
            model_name=model_name,
            rebuild_agent=_rebuild_agent,
        )

    if cmd == "/model":
        return handle_model(
            args,
            settings=settings,
            agent=agent,
            project_root=root,
            thread_id=thread_id,
            apply_thinking_inplace=_apply_thinking_inplace,
            rebuild_agent=_rebuild_agent,
            persist_model_binding=_persist_model_binding,
            mcp_attach_pending=_mcp_attach_pending,
        )

    if cmd == "/fast":
        return handle_fast(args, settings=settings)

    if cmd == "/goal" or _is_goal_alias(cmd):
        from synapse.commands.goal import handle_goal

        return handle_goal(args, thread_id=thread_id, settings=settings)

    if cmd == "/theme":
        return handle_theme(args, settings=settings, project_root=root)

    if cmd == "/compact":
        from synapse.runtime.context_compact import force_compact_via_agent

        ok, lines = force_compact_via_agent(agent, thread_id=thread_id)
        md = "## Compact\n\n" + "\n".join(f"- {x}" for x in lines)
        return SlashResult(handled=True, lines=lines, error=not ok, markdown=md)

    if cmd in {"/compression", "/tool-output", "/tool-compress"}:
        return handle_compression(settings, thread_id, args)

    if cmd == "/context":
        from synapse.runtime.context_compact import context_status_lines

        plain = context_status_lines(agent, thread_id)
        rows = []
        for line in plain:
            if "=" in line:
                k, v = line.split("=", 1)
                rows.append((k.strip(), v.strip()))
            elif ": " in line:
                k, v = line.split(": ", 1)
                rows.append((k.strip(), v.strip()))
            else:
                rows.append(("", line.strip()))
        md = "## Context\n\n| Key | Value |\n|---|---|\n"
        for k, v in rows:
            md += f"| {markdown_escape(k)} | {markdown_escape(v)} |\n"
        return SlashResult(handled=True, lines=plain, markdown=md)

    if cmd == "/safety":
        from synapse.runtime.safety import (
            apply_safety_to_settings,
            format_safety_status,
            get_safety_profile,
        )

        if not args:
            plain = format_safety_status(settings)
            md = "## Safety\n\n| Setting | Value |\n|---|---|\n"
            for line in plain:
                if ": " in line:
                    k, v = line.split(": ", 1)
                    md += f"| {markdown_escape(k.strip())} | {markdown_escape(v.strip())} |\n"
                elif line.startswith("profiles:") or line.startswith("switch:"):
                    md += f"\n*{markdown_escape(line)}*\n"
            return SlashResult(handled=True, lines=plain, markdown=md)
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
        md = "## Safety\n\n" + "\n".join(f"- {n}" for n in notes) + "\n- agent rebuilt"
        return SlashResult(
            handled=True,
            lines=[*notes, "agent rebuilt"],
            markdown=md,
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
        from synapse.content.skills_catalog import (
            discover_skills,
            format_skills_lines,
            skills_paths_from_settings,
        )

        paths = skills_paths_from_settings(settings, root)
        skills = discover_skills(paths)
        plain = format_skills_lines(skills)
        if not skills:
            md = "## Skills\n\n*(none found)*\n\ntip: put `SKILL.md` under `skills/<name>/`"
        else:
            md = f"## Skills ({len(skills)})\n\n| Name | Description | Path |\n|---|---|---|\n"
            for s in skills:
                desc = s.description or "-"
                if len(desc) > 80:
                    desc = desc[:79] + "..."
                md += (
                    f"| {markdown_escape(s.name)} | {markdown_escape(desc)} "
                    f"| `{markdown_escape(s.path)}` |\n"
                )
        return SlashResult(handled=True, lines=plain, markdown=md)

    if cmd == "/memory":
        from synapse.content.skills_catalog import (
            format_memory_lines,
            list_memory_files,
            memory_paths_from_settings,
        )

        paths = memory_paths_from_settings(settings, root)
        entries = list_memory_files(paths)
        plain = format_memory_lines(entries)
        if not entries:
            md = "## Memory\n\n*(no paths configured)*"
        else:
            md = f"## Memory Files ({len(entries)})\n\n| Path | Size | Status |\n|---|---|---|\n"
            for path, exists, size in entries:
                status = "ok" if exists else "missing"
                md += f"| `{markdown_escape(path)}` | {size} | {status} |\n"
            md += "\n*Existing files are injected via `create_deep_agent(memory=...)`*"
        return SlashResult(handled=True, lines=plain, markdown=md)

    if cmd in {"/subagents", "/subagent"}:
        from synapse.runtime.subagents import build_default_subagents, format_subagents_lines

        specs = getattr(agent, "_coding_subagents", None)
        if specs is None:
            # Fallback when the agent has no cached compiled specs (rare). Custom
            # subagents are not loaded here because there is no inherit_tools
            # context; the cached path above already reflects them.
            specs = build_default_subagents(
                enabled=getattr(settings, "enable_subagents", True),
                isolate_tools=True,
                tester_model=getattr(settings, "subagent_tester_model", None),
                reviewer_model=getattr(settings, "subagent_reviewer_model", None),
                researcher_model=getattr(settings, "subagent_researcher_model", None),
                disable_builtin_subagents=getattr(settings, "disable_builtin_subagents", []),
            )
        plain = format_subagents_lines(specs)
        if not specs:
            md = "## Sub-agents\n\n*disabled*"
        else:
            md = f"## Sub-agents ({len(specs)})\n\n"
            md += "| Name | Model | Isolation | Tools |\n|---|---|---|---|\n"
            for spec in specs:
                name = spec.get("name") or "?"
                model = spec.get("model") or "(inherit)"
                tools = spec.get("tools") or []
                tool_names = [getattr(t, "name", getattr(t, "__name__", str(t))) for t in tools]
                mw = spec.get("middleware") or []
                isolation = "tool-exclude" if mw else ("tools+" if tools else "default")
                tool_list = ", ".join(str(n) for n in tool_names) if tool_names else "-"
                md += (
                    f"| {markdown_escape(name)} | {markdown_escape(model)} "
                    f"| {markdown_escape(isolation)} "
                    f"| {markdown_escape(tool_list)} |\n"
                )
        return SlashResult(handled=True, lines=plain, markdown=md)

    if raw.startswith("/"):
        return SlashResult(
            handled=True,
            lines=[f"unknown command: {cmd}", "type /help for commands"],
            error=True,
        )
    return SlashResult(handled=False)
