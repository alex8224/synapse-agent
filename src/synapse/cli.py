"""Typer CLI for the local coding agent."""

from __future__ import annotations

import atexit
import os
from pathlib import Path
from typing import Any

import typer

from synapse.agent import build_coding_agent, default_thread_id
from synapse.config import bootstrap_project_env, load_settings
from synapse.mcp_client import load_mcp_server_configs, load_mcp_tools
from synapse.models_registry import registry_from_settings
from synapse.sessions import SessionStore, format_session_table
from synapse.ui.stream import (
    _extract_usage,
    console,
    extract_last_ai_text,
    print_banner,
    print_error,
    print_final,
    print_info,
    print_user,
    stream_agent,
)

app = typer.Typer(
    name="coding-agent",
    help="Local coding agent built on LangChain Deep Agents (LocalShell, no sandbox).",
    add_completion=False,
    no_args_is_help=True,
)

sessions_app = typer.Typer(help="Manage chat session metadata.")
models_app = typer.Typer(help="List/select configured model profiles.")
mcp_app = typer.Typer(help="Inspect MCP server configuration and tools.")
app.add_typer(sessions_app, name="sessions")
app.add_typer(models_app, name="models")
app.add_typer(mcp_app, name="mcp")


def _bootstrap_env() -> Path | None:
    """Load project `.env` with override=True so it beats stale system keys."""
    try:
        from synapse.prompts import ensure_user_system_prompt

        ensure_user_system_prompt()
    except Exception:  # noqa: BLE001
        pass
    return bootstrap_project_env(Path.cwd())


def _resolve_settings(
    *,
    workspace: Path | None,
    model: str | None,
    require_approval: bool | None,
    debug: bool,
    readonly: bool | None = None,
):
    overrides: dict = {"debug": debug}
    if workspace is not None:
        overrides["workspace"] = workspace
    if model is not None:
        overrides["model"] = model
        overrides["active_model"] = model
    if require_approval is not None:
        overrides["require_approval"] = require_approval
    if readonly is not None:
        overrides["readonly"] = readonly
    return load_settings(**overrides)


def _session_store(settings) -> SessionStore:
    return SessionStore(settings.resolved_sessions_path())


def _print_auth_context(settings, env_path: Path | None) -> None:
    if env_path is not None:
        print_info(f"loaded env: {env_path}")
    print_info(
        f"auth: key={settings.mask_openai_key()} base_url={settings.openai_base_url!r}"
    )


def _setup_readline(history_file: Path, settings=None) -> None:
    """Enable readline-style command history + slash completion for chat."""
    try:
        import readline
    except ImportError:
        try:
            import pyreadline3 as readline  # type: ignore[no-redef]
        except ImportError:
            return

    readline.set_history_length(1000)
    history_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        readline.read_history_file(str(history_file))
    except (FileNotFoundError, OSError):
        pass
    atexit.register(readline.write_history_file, str(history_file))

    if settings is None:
        return

    from synapse.slash_complete import build_complete_context, complete_slash

    def _completer(text: str, state: int) -> str | None:
        try:
            buf = readline.get_line_buffer()
        except Exception:  # noqa: BLE001
            buf = text
        if not (buf or "").startswith("/"):
            return None
        ctx = build_complete_context(settings)
        cands = complete_slash(buf, ctx)
        if not cands and " " in buf.rstrip():
            cands = complete_slash(buf.rstrip().rsplit(" ", 1)[0] + " ", ctx)
        matches = [c for c in cands if c.casefold().startswith(buf.casefold())] or cands
        if state < len(matches):
            return matches[state]
        return None

    try:
        readline.set_completer(_completer)
        # Treat the whole buffer as one token so multi-arg slash lines complete.
        if hasattr(readline, "set_completer_delims"):
            readline.set_completer_delims("")
        readline.parse_and_bind("tab: complete")
    except Exception:  # noqa: BLE001
        pass


def _print_auth_error(settings, exc: Exception) -> None:
    msg = str(exc)
    print_error(msg)
    if "401" in msg or "Invalid token" in msg or "Unauthorized" in msg:
        print_info(
            "Auth failed. Check project .env OPENAI_API_KEY / OPENAI_BASE_URL. "
            "Project .env now overrides system env; re-check key validity on gateway."
        )
        print_info(
            f"Using key {settings.mask_openai_key()} "
            f"base_url={settings.openai_base_url!r} model={settings.model!r}"
        )


def _print_tokens_from_state(state: dict) -> None:
    """Print token usage summary from agent final state."""
    messages = state.get("messages") if isinstance(state, dict) else []
    if not messages:
        return
    seen: set[str] = set()
    total_in = 0
    total_out = 0
    for msg in messages:
        msg_id = getattr(msg, "id", None) or id(msg)
        key = f"usage:{msg_id}"
        if key in seen:
            continue
        u = _extract_usage(msg)
        if u["input_tokens"] or u["output_tokens"]:
            seen.add(key)
            total_in += u["input_tokens"]
            total_out += u["output_tokens"]
    if total_in or total_out:
        total = total_in + total_out
        print_info(f"tokens: {total} (in={total_in} out={total_out})")


def _run_once(
    agent,
    payload: dict | Any,
    config: dict,
    *,
    use_stream: bool = True,
    token_stream: bool = True,
    max_concurrency: int = 8,
    sink=None,
) -> tuple[str, bool, Any]:
    """Execute one turn.

    Returns:
        (answer_text, already_displayed, stream_result_or_none)
    """
    if use_stream:
        streamed = stream_agent(
            agent,
            payload,
            config,
            token_stream=token_stream,
            prefer_async=True,
            max_concurrency=max_concurrency,
            sink=sink,
        )
        if streamed.final_text:
            return streamed.final_text, streamed.streamed_answer, streamed
        if streamed.state.get("messages"):
            return extract_last_ai_text(streamed.state), False, streamed
        if streamed.interrupted:
            return "", True, streamed
        print_info("stream empty, falling back to invoke...")
    else:
        print_info("running...")

    invoked = agent.invoke(payload, config=config)
    state = invoked if isinstance(invoked, dict) else {"messages": invoked}
    _print_tokens_from_state(state)
    return (
        extract_last_ai_text(state),
        False,
        None,
    )


def _resume_hitl(
    agent,
    config: dict,
    *,
    action: str,
    message: str | None = None,
    token_stream: bool = True,
    max_concurrency: int = 8,
) -> tuple[str, bool]:
    """Resume a graph paused for HITL."""
    from synapse.hitl import (
        build_decisions,
        build_resume_payload,
        extract_pending_interrupt,
        format_interrupt_lines,
    )

    pending = extract_pending_interrupt(agent, config)
    if pending is None or (not pending.actions and not pending.raw):
        print_info("no pending approval")
        return "", True
    for line in format_interrupt_lines(pending):
        print_info(line)
    decisions = build_decisions(pending, action=action, message=message)
    payload = build_resume_payload(decisions)
    answer, already, streamed = _run_once(
        agent,
        payload,
        config,
        use_stream=True,
        token_stream=token_stream,
        max_concurrency=max_concurrency,
    )
    if streamed is not None and streamed.interrupted:
        print_info("still waiting for approval — /approve or /reject")
    return answer, already


def _handle_chat_command(
    text: str,
    *,
    agent,
    settings,
    store: SessionStore,
    tid: str,
    config: dict,
) -> tuple[object, str, dict, bool]:
    """Handle slash commands. Returns (agent, tid, config, handled)."""
    from synapse.slash_cmds import handle_slash

    result = handle_slash(
        text,
        settings=settings,
        agent=agent,
        thread_id=tid,
        project_root=Path.cwd(),
    )
    if not result.handled:
        return agent, tid, config, False
    if result.exit_requested:
        for line in result.lines:
            print_info(line)
        raise SystemExit(0)
    if result.thread_id is not None:
        tid = result.thread_id
        config = {
            "configurable": {"thread_id": tid},
            "max_concurrency": settings.max_concurrency,
        }
        # /new allocates an id without persisting; /switch may need ensure.
        if result.reload_transcript or store.get(tid) is not None:
            store.ensure(tid, model=settings.model)
    if result.agent is not None:
        agent = result.agent
    printer = print_error if result.error else print_info
    for line in result.lines:
        printer(line)

    # HITL resume from slash
    if getattr(result, "resume_action", None):
        answer, already, _streamed = _resume_hitl(
            agent,
            config,
            action=result.resume_action or "approve",
            message=getattr(result, "resume_message", None),
            token_stream=settings.token_stream,
            max_concurrency=settings.max_concurrency,
        )
        if answer and not already:
            print_final(answer)
    return agent, tid, config, True


@app.command("run")
def run_cmd(
    task: str = typer.Argument(..., help="Task for the coding agent"),
    workspace: Path | None = typer.Option(
        None, "--workspace", "-w", help="Workspace directory", exists=False, file_okay=False
    ),
    model: str | None = typer.Option(
        None, "--model", "-m", help="Model profile alias or provider:model"
    ),
    require_approval: bool = typer.Option(
        False,
        "--require-approval/--no-require-approval",
        help="Enable HITL approval (default: disabled, auto-pass)",
    ),
    readonly: bool = typer.Option(
        False, "--readonly/--no-readonly", help="Exclude write/execute tools via harness"
    ),
    thread_id: str | None = typer.Option(None, "--thread-id", help="Resume a session id"),
    debug: bool = typer.Option(False, "--debug", help="Enable deepagents debug mode"),
    stream: bool = typer.Option(
        True, "--stream/--no-stream", help="Stream intermediate updates"
    ),
) -> None:
    """Run a single coding task and exit."""
    env_path = _bootstrap_env()
    settings = _resolve_settings(
        workspace=workspace,
        model=model,
        require_approval=require_approval,
        debug=debug,
        readonly=readonly,
    )
    print_banner(str(settings.workspace), settings.model, settings.require_approval)
    _print_auth_context(settings, env_path)

    try:
        agent = build_coding_agent(
            settings,
            project_root=Path.cwd(),
            load_mcp=bool(settings.enable_mcp),
        )
    except Exception as exc:  # noqa: BLE001
        print_error(f"failed to build agent: {exc}")
        raise typer.Exit(code=1) from exc

    tid = thread_id or default_thread_id()
    store = _session_store(settings)
    store.touch(tid, title_hint=task, model=settings.model)
    config = {
        "configurable": {"thread_id": tid},
        "max_concurrency": settings.max_concurrency,
    }
    payload = {"messages": [{"role": "user", "content": task}]}
    print_info(f"thread_id={tid}")
    print_info(
        f"stream: token={settings.token_stream} parallel_tools={settings.parallel_tool_calls} "
        f"max_concurrency={settings.max_concurrency}"
    )
    print_user(task)

    try:
        answer, already, streamed = _run_once(
            agent,
            payload,
            config,
            use_stream=stream,
            token_stream=settings.token_stream,
            max_concurrency=settings.max_concurrency,
        )
        # Interactive HITL loop for one-shot run when approval is enabled.
        while streamed is not None and getattr(streamed, "interrupted", False):
            print_info("waiting for approval — type approve / reject [reason]")
            try:
                choice = input("hitl> ").strip()
            except (EOFError, KeyboardInterrupt):
                print_info(f"left pending. resume later with thread_id={tid}")
                raise typer.Exit(code=2) from None
            if not choice:
                continue
            low = choice.split(maxsplit=1)
            action = low[0].casefold().lstrip("/")
            if action in {"a", "ok", "yes", "y", "approve"}:
                action = "approve"
                reason = None
            elif action in {"r", "no", "n", "reject"}:
                action = "reject"
                reason = low[1].strip() if len(low) > 1 else None
            elif action in {"q", "quit", "exit"}:
                print_info(f"left pending. resume later with thread_id={tid}")
                raise typer.Exit(code=2)
            else:
                print_info("use: approve | reject [reason] | quit")
                continue
            answer, already, streamed = _resume_hitl(
                agent,
                config,
                action=action,
                message=reason,
                token_stream=settings.token_stream,
                max_concurrency=settings.max_concurrency,
            )
    except typer.Exit:
        raise
    except Exception as exc:  # noqa: BLE001
        _print_auth_error(settings, exc)
        raise typer.Exit(code=1) from exc

    if answer:
        if not already:
            print_final(answer)
    elif streamed is not None and getattr(streamed, "interrupted", False):
        print_info(f"still pending approval. thread_id={tid}")
    else:
        print_final("(empty response)")
    print_info(f"done. thread_id={tid}")


@app.command("chat")
def chat_cmd(
    workspace: Path | None = typer.Option(
        None, "--workspace", "-w", help="Workspace directory", exists=False, file_okay=False
    ),
    model: str | None = typer.Option(
        None, "--model", "-m", help="Model profile alias or provider:model"
    ),
    require_approval: bool = typer.Option(
        False,
        "--require-approval/--no-require-approval",
        help="Enable HITL approval (default: disabled, auto-pass)",
    ),
    readonly: bool = typer.Option(
        False, "--readonly/--no-readonly", help="Exclude write/execute tools via harness"
    ),
    thread_id: str | None = typer.Option(None, "--thread-id", help="Resume a session id"),
    debug: bool = typer.Option(False, "--debug", help="Enable deepagents debug mode"),
) -> None:
    """Interactive multi-turn chat with checkpointed session state."""
    env_path = _bootstrap_env()
    settings = _resolve_settings(
        workspace=workspace,
        model=model,
        require_approval=require_approval,
        debug=debug,
        readonly=readonly,
    )
    print_banner(str(settings.workspace), settings.model, settings.require_approval)
    _print_auth_context(settings, env_path)

    _history_file = Path.cwd() / ".coding-agent" / "history"
    _setup_readline(_history_file)

    store = _session_store(settings)
    from synapse.sessions import (
        apply_binding_to_settings,
        binding_from_settings,
        pick_startup_thread_id,
        resolve_startup_binding,
    )

    try:
        store.prune_empty()
    except Exception:  # noqa: BLE001
        pass
    tid, resumed = pick_startup_thread_id(store, thread_id, resume_last=True)
    # Restore session / last-used model unless CLI --model was given.
    if not model:
        binding = resolve_startup_binding(
            store, thread_id=tid if resumed else None, cli_model=model
        )
        if binding is not None:
            apply_binding_to_settings(settings, binding)

    try:
        agent = build_coding_agent(settings, project_root=Path.cwd())
    except Exception as exc:  # noqa: BLE001
        print_error(f"failed to build agent: {exc}")
        raise typer.Exit(code=1) from exc

    bind = binding_from_settings(settings)
    # Persist session metadata only after the first real user turn (store.touch).
    store.set_last_model_binding(bind)
    config = {
        "configurable": {"thread_id": tid},
        "max_concurrency": settings.max_concurrency,
    }
    if resumed:
        print_info(f"thread_id={tid}  (resumed; type /help for commands)")
    else:
        print_info(f"thread_id={tid}  (new; saved on first message; type /help)")
    print_info(f"model={bind.display()}")
    print_info(
        f"stream: token={settings.token_stream} parallel_tools={settings.parallel_tool_calls} "
        f"max_concurrency={settings.max_concurrency}"
    )
    console.print("Enter a task. Multi-turn memory is enabled via checkpointer.\n")


    while True:
        try:
            text = console.input("[bold cyan]You>[/bold cyan] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            print_info("bye")
            break

        if not text:
            continue

        try:
            agent, tid, config, handled = _handle_chat_command(
                text,
                agent=agent,
                settings=settings,
                store=store,
                tid=tid,
                config=config,
            )
        except SystemExit:
            break
        if handled:
            continue

        bind = binding_from_settings(settings)
        store.touch(
            tid,
            title_hint=text,
            model=bind.model,
            active_model=bind.active_model,
            thinking=bind.thinking,
        )

        payload = {"messages": [{"role": "user", "content": text}]}
        try:
            answer, already, streamed = _run_once(
                agent,
                payload,
                config,
                use_stream=True,
                token_stream=settings.token_stream,
                max_concurrency=settings.max_concurrency,
            )
            if answer:
                if not already:
                    print_final(answer)
            elif streamed is not None and streamed.interrupted:
                print_info("waiting for approval — /approve or /reject")
            else:
                print_final("(empty response)")
        except Exception as exc:  # noqa: BLE001
            _print_auth_error(settings, exc)


@app.command("tui")
def tui_cmd(
    workspace: Path | None = typer.Option(
        None, "--workspace", "-w", help="Workspace directory", exists=False, file_okay=False
    ),
    model: str | None = typer.Option(
        None, "--model", "-m", help="Model profile alias or provider:model"
    ),
    require_approval: bool = typer.Option(
        False,
        "--require-approval/--no-require-approval",
        help="Enable HITL approval (default: disabled, auto-pass)",
    ),
    readonly: bool = typer.Option(
        False, "--readonly/--no-readonly", help="Exclude write/execute tools via harness"
    ),
    thread_id: str | None = typer.Option(None, "--thread-id", help="Resume a session id"),
    debug: bool = typer.Option(False, "--debug", help="Enable deepagents debug mode"),
) -> None:
    """Full-screen Textual TUI chat (rich remains the CLI renderer)."""
    env_path = _bootstrap_env()
    settings = _resolve_settings(
        workspace=workspace,
        model=model,
        require_approval=require_approval,
        debug=debug,
        readonly=readonly,
    )
    try:
        from synapse.ui.tui import run_tui
    except ImportError as exc:  # pragma: no cover - dependency missing
        print_error(f"textual is required for TUI mode: {exc}")
        print_info("Install with: uv add textual  (or uv sync)")
        raise typer.Exit(code=1) from exc

    try:
        run_tui(
            settings=settings,
            thread_id=thread_id,
            env_path=env_path,
            project_root=Path.cwd(),
            cli_model=model,
        )

    except Exception as exc:  # noqa: BLE001
        _print_auth_error(settings, exc)
        raise typer.Exit(code=1) from exc


@sessions_app.command("list")
def sessions_list(
    limit: int = typer.Option(50, "--limit", "-n", help="Max sessions"),
    all_sessions: bool = typer.Option(
        False,
        "--all",
        help="Include empty placeholder sessions (default: hide them)",
    ),
) -> None:
    """List recent sessions."""
    settings = load_settings()
    store = _session_store(settings)
    items = store.list(limit=limit) if all_sessions else store.list_nonempty(limit=limit)
    console.print(format_session_table(items))


@sessions_app.command("prune")
def sessions_prune() -> None:
    """Delete empty placeholder sessions (never got a real first message)."""
    settings = load_settings()
    store = _session_store(settings)
    deleted = store.prune_empty()
    print_info(f"pruned {len(deleted)} empty session(s)")
    for tid in deleted[:20]:
        print_info(f"  - {tid}")
    if len(deleted) > 20:
        print_info(f"  … and {len(deleted) - 20} more")


@sessions_app.command("delete")
def sessions_delete(
    thread_id: str = typer.Argument(..., help="Session thread id"),
) -> None:
    """Delete session metadata (checkpoint rows are left to LangGraph GC)."""
    settings = load_settings()
    store = _session_store(settings)
    ok = store.delete(thread_id)
    if ok:
        print_info(f"deleted session metadata: {thread_id}")
    else:
        print_error(f"session not found: {thread_id}")
        raise typer.Exit(code=1)


@sessions_app.command("rename")
def sessions_rename(
    thread_id: str = typer.Argument(..., help="Session thread id"),
    title: str = typer.Argument(..., help="New title"),
) -> None:
    """Rename a session."""
    settings = load_settings()
    store = _session_store(settings)
    info = store.rename(thread_id, title)
    if info is None:
        print_error(f"session not found: {thread_id}")
        raise typer.Exit(code=1)
    print_info(f"renamed {thread_id} -> {info.title}")


@sessions_app.command("export")
def sessions_export(
    thread_id: str = typer.Argument(..., help="Session thread id"),
    fmt: str = typer.Option("md", "--format", "-f", help="md or json"),
    out: Path | None = typer.Option(
        None,
        "--out",
        "-o",
        help="Output file (default: .coding-agent/exports/<thread_id>.md|json)",
    ),
    full: bool = typer.Option(
        True,
        "--full/--meta-only",
        help="Include checkpoint transcript when available",
    ),
    stdout: bool = typer.Option(
        False,
        "--stdout",
        help="Print export body to stdout instead of writing a file",
    ),
) -> None:
    """Export session transcript to a file (default). Use --stdout to pipe."""
    import json as _json

    from synapse.transcript import (
        export_transcript_json,
        export_transcript_markdown,
        load_messages_from_sqlite_file,
    )

    settings = load_settings()
    store = _session_store(settings)
    info = store.get(thread_id)
    if info is None:
        print_error(f"session not found: {thread_id}")
        raise typer.Exit(code=1)

    messages = []
    if full and settings.checkpoint_backend == "sqlite":
        messages = load_messages_from_sqlite_file(settings.checkpoint_path, thread_id)

    fmt_n = "json" if fmt.lower() in {"json", "j"} else "md"
    if fmt_n == "json":
        if full:
            data = export_transcript_json(
                thread_id=thread_id,
                title=info.title,
                model=info.model,
                messages=messages,
                meta=info.to_dict(),
            )
        else:
            data = info.to_dict()
        text = _json.dumps(data, ensure_ascii=False, indent=2)
    else:
        if full:
            text = export_transcript_markdown(
                thread_id=thread_id,
                title=info.title,
                model=info.model,
                messages=messages,
            )
            if not messages:
                text = (store.export_markdown(thread_id) or "") + (
                    "\n## Transcript\n\n(no checkpoint messages found)\n"
                )
        else:
            text = store.export_markdown(thread_id) or ""

    if stdout:
        console.print(text)
        return

    if out is None:
        safe = "".join(
            c if c.isalnum() or c in "-_" else "_" for c in (thread_id or "session")
        )
        safe = (safe or "session")[:80]
        out = (
            Path(settings.checkpoint_path).expanduser().resolve().parent
            / "exports"
            / f"{safe}.{fmt_n}"
        )
    out = Path(out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print_info(f"wrote {out} (messages={len(messages)})")


@sessions_app.command("search")
def sessions_search(
    query: str = typer.Argument(..., help="Keyword"),
    limit: int = typer.Option(50, "--limit", "-n"),
) -> None:
    """Search session titles/summaries."""
    settings = load_settings()
    store = _session_store(settings)
    console.print(format_session_table(store.search(query, limit=limit)))


@models_app.command("list")
def models_list() -> None:
    """List model profiles."""
    settings = load_settings()
    reg = registry_from_settings(settings)
    print_info(f"default={reg.default}")
    if settings.models_config_path:
        print_info(f"config={settings.models_config_path}")
    for name in reg.list_names():
        p = reg.get(name)
        print_info(
            f"{name}: {p.model} thinking={p.thinking_label()} "
            f"base_url={p.base_url!r}"
        )
        if p.extra:
            print_info(f"  params={p.extra}")
        if p.model_kwargs:
            print_info(f"  model_kwargs={p.model_kwargs}")
        if p.extra_body:
            print_info(f"  extra_body={p.extra_body}")


@mcp_app.command("list")
def mcp_list() -> None:
    """List configured MCP servers."""
    settings = load_settings()
    servers = load_mcp_server_configs(
        path=settings.mcp_config_path,
        json_blob=settings.mcp_servers_json,
    )
    if not servers:
        print_info("no MCP servers configured")
        return
    for s in servers:
        print_info(
            f"{s.name}: transport={s.transport} enabled={s.enabled} "
            f"command={s.command!r} url={s.url!r}"
        )


@mcp_app.command("test")
def mcp_test() -> None:
    """Try connecting to configured MCP servers and list tools."""
    settings = load_settings()
    servers = load_mcp_server_configs(
        path=settings.mcp_config_path,
        json_blob=settings.mcp_servers_json,
    )
    if not servers:
        print_info("no MCP servers configured")
        return
    result = load_mcp_tools(servers, enabled=True)
    print_info(f"loaded servers: {', '.join(result.servers) or '-'}")
    print_info(f"tools: {len(result.tools)}")
    for tool in result.tools:
        print_info(f"- {getattr(tool, 'name', tool)}")
    for w in result.warnings:
        print_info(f"warn: {w}")


@app.command("version")
def version_cmd() -> None:
    """Print package version."""
    from synapse import __version__

    console.print(__version__)


def main() -> None:
    """Console script entrypoint."""
    os.environ.setdefault("PYTHONUTF8", "1")
    try:
        from synapse.startup_trace import ensure_started, mark

        ensure_started()
        mark("cli:main")
    except Exception:  # noqa: BLE001
        pass
    app()


if __name__ == "__main__":
    main()
