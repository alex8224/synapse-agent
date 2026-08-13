"""Typer CLI for the local coding agent."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import typer

from synapse.projects.catalog import ProjectCatalog, ProjectInfo
from synapse.sessions.store import SessionStore, format_session_table
from synapse.settings import bootstrap_project_env, load_settings
from synapse.ui.stream import (
    console,
    extract_last_ai_text,
    print_banner,
    print_error,
    print_final,
    print_info,
    stream_agent,
)

app = typer.Typer(
    name="synapse",
    help="Local coding agent built on LangChain Deep Agents (LocalShell, no sandbox).",
    add_completion=False,
    no_args_is_help=False,
)

sessions_app = typer.Typer(help="Manage chat session metadata.")
models_app = typer.Typer(help="List/select configured model profiles.")
mcp_app = typer.Typer(help="Inspect MCP server configuration and tools.")
tool_output_app = typer.Typer(help="Inspect reversible tool-output transformation metrics.")
auth_app = typer.Typer(help="Manage provider authentication.")
auth_openai_app = typer.Typer(help="Manage OpenAI Codex OAuth authentication.")
projects_app = typer.Typer(help="Global project catalog (cross-project sessions and runs).")
app.add_typer(sessions_app, name="sessions")
app.add_typer(models_app, name="models")
app.add_typer(mcp_app, name="mcp")
app.add_typer(tool_output_app, name="tool-output")
auth_app.add_typer(auth_openai_app, name="openai")
app.add_typer(auth_app, name="auth")
app.add_typer(projects_app, name="projects")


# ---------------------------------------------------------------------------
# Common helpers
# ---------------------------------------------------------------------------


def _bootstrap_env() -> Path | None:
    """Load project `.env` with override=True so it beats stale system keys."""
    try:
        from synapse.content.prompts import ensure_user_system_prompt

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


def _print_settings_error(exc: Exception) -> None:
    """Print a concise, actionable configuration error without a traceback."""
    print_error(f"Configuration error: {exc}")
    print_info(
        "Check models.json, settings.json, and inline JSON environment variables "
        "(MODELS_JSON / MCP_SERVERS_JSON)."
    )


# ---------------------------------------------------------------------------
# Authentication commands
# ---------------------------------------------------------------------------


@auth_openai_app.command("login")
def auth_openai_login(
    import_codex: bool = typer.Option(
        False, "--import-codex", help="Import an existing ~/.codex/auth.json OAuth grant"
    ),
    no_browser: bool = typer.Option(
        False, "--no-browser", help="Do not open the browser automatically"
    ),
    timeout: float = typer.Option(
        300.0, "--timeout", min=30.0, max=900.0, help="Login timeout in seconds"
    ),
) -> None:
    """Sign in with a ChatGPT account for a Codex OAuth model profile."""
    from synapse.integrations.openai_oauth import (
        OpenAIOAuthStore,
        import_codex_credentials,
        login_via_browser,
    )

    try:
        tokens = (
            import_codex_credentials()
            if import_codex
            else login_via_browser(timeout_seconds=timeout, open_browser=not no_browser)
        )
        store = OpenAIOAuthStore()
        store.save(tokens)
    except Exception as exc:  # noqa: BLE001
        print_error(str(exc))
        raise typer.Exit(code=1) from exc
    account = tokens.account_id or "available"
    print_info(f"OpenAI Codex OAuth login saved: account={account}")
    print_info('Configure a model profile with: "auth": "openai_oauth"')


@auth_openai_app.command("status")
def auth_openai_status() -> None:
    """Show OAuth login state without exposing credential values."""
    from datetime import datetime

    from synapse.integrations.openai_oauth import OpenAIOAuthStore

    try:
        tokens = OpenAIOAuthStore().load()
    except Exception as exc:  # noqa: BLE001
        print_error(str(exc))
        raise typer.Exit(code=1) from exc
    if tokens is None:
        print_info("OpenAI Codex OAuth: not logged in")
        return
    expiry = datetime.fromtimestamp(tokens.expires_at).astimezone().isoformat(timespec="seconds")
    state = "refresh required" if tokens.is_expiring else "active"
    print_info(
        f"OpenAI Codex OAuth: {state}; account={tokens.account_id or 'unknown'}; expires={expiry}"
    )


@auth_openai_app.command("logout")
def auth_openai_logout() -> None:
    """Remove Synapse's locally stored OpenAI OAuth grant."""
    from synapse.integrations.openai_oauth import OpenAIOAuthStore

    removed = OpenAIOAuthStore().delete()
    message = (
        "OpenAI Codex OAuth credentials removed" if removed else "OpenAI Codex OAuth: not logged in"
    )
    print_info(message)


# ---------------------------------------------------------------------------
# TUI launch helpers
# ---------------------------------------------------------------------------


async def _enhance_task(
    *,
    agent: Any,
    settings: Any,
    task: str,
) -> str:
    """Enrich the task with long-term memory, RAG knowledge, and planning.

    Returns the original task if all enhancements are disabled or fail.
    """
    enhanced = task
    context_parts: list[str] = []

    # 1. RAG: search project knowledge base
    _kb = getattr(agent, "_coding_knowledge_base", None)
    if _kb is not None and getattr(settings, "enable_rag", False):
        try:
            chunks = await _kb.search(task, top_k=settings.rag_top_k)
            if chunks:
                ctx = "## 项目知识库相关片段\n" + "\n".join(
                    f"  [{c['source']}] {c['text'][:800]}" for c in chunks
                )
                context_parts.append(ctx)
        except Exception:  # noqa: BLE001
            pass

    # 2. Long-term memory: recall relevant past interactions
    _ltm = getattr(agent, "_coding_long_term_memory", None)
    if _ltm is not None and getattr(settings, "enable_long_term_memory", False):
        try:
            entries = await _ltm.recall(task, top_k=3)
            if entries:
                ctx = "## 相关历史记忆\n" + "\n".join(
                    f"- {e.text[:500]}" for e in entries
                )
                context_parts.append(ctx)
        except Exception:  # noqa: BLE001
            pass

    # Prepend context to the task
    if context_parts:
        enhanced = "\n\n".join(context_parts) + "\n\n---\n\n" + enhanced

    return enhanced


async def _auto_record_memory(
    *,
    ltm: Any,
    model: Any = None,
    task: str,
    answer: str,
    thread_id: str = "",
) -> int:
    """Auto-record valuable lessons after a completed turn.

    Uses ``AutoRecorder`` with heuristic pre-filter + optional LLM extraction.
    Returns the number of stored lessons.
    """
    from synapse.memory.auto_recorder import AutoRecorder

    recorder = AutoRecorder(model=model)
    return await recorder.record_if_valuable(
        ltm,
        task=task,
        answer=answer,
        thread_id=thread_id,
    )


def _resolve_launch_target(
    *,
    workspace: Path | None,
    session: str | None,
    project: str | None,
    model: str | None,
    require_approval: bool,
    readonly: bool,
    debug: bool,
) -> tuple[dict, str | None, Path | None]:
    """Resolve ``--session <project>:<thread>`` / ``--project <ref>`` to launch args.

    Priority (P7): explicit global session > explicit project/workspace >
    cwd is a registered project > plain workspace defaults.

    Returns ``(overrides, thread_id, project_root)``; ``project_root`` is set
    when a concrete project was chosen, otherwise None (cwd default).
    """
    overrides: dict = {"debug": debug}
    overrides["model"] = model
    overrides["workspace"] = workspace
    if model is not None:
        overrides["active_model"] = model
    if require_approval is not None:
        overrides["require_approval"] = require_approval
    if readonly is not None:
        overrides["readonly"] = readonly
    thread_id: str | None = None

    if session:
        try:
            from synapse.runtime.sessions import parse_global_id, resolve_session_ref

            ref = parse_global_id(session)
            catalog = ProjectCatalog(load_settings().resolved_catalog_path())
            resolved = resolve_session_ref(session, catalog=catalog, verify=True)
            info = catalog.get_project(project_id=resolved.project_id)
            if info is None:
                print_error(f"project not found: {resolved.project_id}")
                raise typer.Exit(code=1)
            overrides["workspace"] = info.workspace_path
            thread_id = ref.thread_id
        except Exception as exc:  # noqa: BLE001 - fall through to explicit project
            if isinstance(exc, typer.Exit):
                raise
            print_error(f"cannot resolve --session {session!r}: {exc}")
            raise typer.Exit(code=1) from exc

    if project and overrides.get("workspace") is None:
        try:
            catalog = ProjectCatalog(load_settings().resolved_catalog_path())
            info = catalog.resolve_project(project)
            if info is None:
                print_error(f"project not found: {project}")
                raise typer.Exit(code=1)
            overrides["workspace"] = info.workspace_path
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, typer.Exit):
                raise
            print_error(f"cannot resolve --project {project!r}: {exc}")
            raise typer.Exit(code=1) from exc

    if workspace is not None:
        overrides["workspace"] = workspace

    root: Path | None = None
    if overrides.get("workspace") is not None:
        root = Path(overrides["workspace"]).expanduser().resolve()
    return overrides, thread_id, root


def _launch_tui(
    *,
    workspace: Path | None,
    model: str | None,
    require_approval: bool,
    readonly: bool,
    thread_id: str | None,
    debug: bool,
    session: str | None = None,
    project: str | None = None,
) -> None:
    """Bootstrap settings and open the full-screen Textual TUI.

    Loops when the TUI exits with a cross-project switch request so the new
    project is launched in a fresh app (single-process boundary, ADR-010).
    """
    from synapse.observability.startup_trace import span

    try:
        with span("cli:import.tui"):
            from synapse.ui.tui import run_tui
    except ImportError as exc:  # pragma: no cover - dependency missing
        print_error(f"textual is required for TUI mode: {exc}")
        print_info("Install with: uv add textual  (or uv sync)")
        raise typer.Exit(code=1) from exc

    with span("cli:launch_target"):
        overrides, resolved_thread, root = _resolve_launch_target(
            workspace=workspace,
            session=session,
            project=project,
            model=model,
            require_approval=require_approval,
            readonly=readonly,
            debug=debug,
        )
    thread_id = thread_id or resolved_thread

    switch_round = 0
    while True:
        try:
            with span("cli:env"):
                env_path = _bootstrap_env()
            with span("cli:settings"):
                settings = _resolve_settings(**overrides)
        except (OSError, ValueError) as exc:
            _print_settings_error(exc)
            raise typer.Exit(code=1) from exc

        try:
            result = run_tui(
                settings=settings,
                thread_id=thread_id,
                env_path=env_path,
                project_root=root,
                cli_model=model,
            )
        except Exception as exc:  # noqa: BLE001
            _print_auth_error(settings, exc)
            raise typer.Exit(code=1) from exc

        # Cross-project switch from the drawer: restart into the target.
        if (
            isinstance(result, (tuple, list))
            and len(result) >= 2
            and result[0] == "switch_project"
        ):
            switch_round += 1
            if switch_round > 8:
                print_error("too many project switches; aborting")
                raise typer.Exit(code=1)
            project_id = str(result[1])
            next_thread = str(result[2]) if len(result) > 2 and result[2] else None
            try:
                catalog = ProjectCatalog(load_settings().resolved_catalog_path())
                info = catalog.get_project(project_id=project_id)
                if info is None:
                    print_error(f"project not found: {project_id}")
                    raise typer.Exit(code=1)
                overrides = {**overrides, "workspace": info.workspace_path}
                root = Path(info.workspace_path).expanduser().resolve()
                thread_id = next_thread
                continue
            except typer.Exit:
                raise
            except Exception as exc:  # noqa: BLE001
                print_error(f"project switch failed: {exc}")
                raise typer.Exit(code=1) from exc
        return


@app.callback(invoke_without_command=True)
def _default_tui(
    ctx: typer.Context,
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
    session: str | None = typer.Option(
        None,
        "--session",
        help="Open a global session '<project_id>:<thread_id>' (any registered project)",
    ),
    project: str | None = typer.Option(
        None, "--project", help="Open a registered project by id prefix, name, or path"
    ),
) -> None:
    """Full-screen Textual TUI - the default interface."""
    if ctx.invoked_subcommand is not None:
        return
    _launch_tui(
        workspace=workspace,
        model=model,
        require_approval=require_approval,
        readonly=readonly,
        thread_id=thread_id,
        debug=debug,
        session=session,
        project=project,
    )


@app.command("tui")
def tui(
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
    session: str | None = typer.Option(
        None,
        "--session",
        help="Open a global session '<project_id>:<thread_id>' (any registered project)",
    ),
    project: str | None = typer.Option(
        None, "--project", help="Open a registered project by id prefix, name, or path"
    ),
) -> None:
    """Launch the full-screen Textual TUI."""
    _launch_tui(
        workspace=workspace,
        model=model,
        require_approval=require_approval,
        readonly=readonly,
        thread_id=thread_id,
        debug=debug,
        session=session,
        project=project,
    )


# ---------------------------------------------------------------------------
# Default callback: launch TUI when no subcommand is given
# ---------------------------------------------------------------------------


@app.callback(invoke_without_command=True)
def _default_tui(
    ctx: typer.Context,
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
    session: str | None = typer.Option(
        None,
        "--session",
        help="Open a global session '<project_id>:<thread_id>' (any registered project)",
    ),
    project: str | None = typer.Option(
        None, "--project", help="Open a registered project by id prefix, name, or path"
    ),
) -> None:
    """Full-screen Textual TUI - the default interface."""
    if ctx.invoked_subcommand is not None:
        return
    _launch_tui(
        workspace=workspace,
        model=model,
        require_approval=require_approval,
        readonly=readonly,
        thread_id=thread_id,
        debug=debug,
        session=session,
        project=project,
    )


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
    session: str | None = typer.Option(
        None,
        "--session",
        help="Open a global session '<project_id>:<thread_id>' (any registered project)",
    ),
    project: str | None = typer.Option(
        None, "--project", help="Open a registered project by id prefix, name, or path"
    ),
) -> None:
    """Full-screen Textual TUI - the default interface."""
    _launch_tui(
        workspace=workspace,
        model=model,
        require_approval=require_approval,
        readonly=readonly,
        thread_id=thread_id,
        debug=debug,
        session=session,
        project=project,
    )


@app.command("transcript-migration-worker", hidden=True)
def transcript_migration_worker(
    checkpoint_path: Path = typer.Option(
        ..., "--checkpoint-path", help="Legacy checkpoint SQLite file"
    ),
    projection_path: Path = typer.Option(
        ..., "--projection-path", help="Transcript projection SQLite file"
    ),
    thread_id: str = typer.Option(..., "--thread-id", help="Thread id to migrate"),
) -> None:
    """Internal: build one legacy transcript projection in a disposable process."""
    from synapse.sessions.transcript_migration import run_transcript_migration_worker

    raise typer.Exit(
        code=run_transcript_migration_worker(
            checkpoint_path=checkpoint_path,
            projection_path=projection_path,
            thread_id=thread_id,
        )
    )


# ---------------------------------------------------------------------------
# One-shot run command (headless, for scripts / evals)
# ---------------------------------------------------------------------------


def _print_tokens_from_state(state: dict) -> None:
    """Print token usage summary from agent final state."""
    from synapse.ui.stream_events import _extract_usage

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
        usage = _extract_usage(msg)
        if usage["input_tokens"] or usage["output_tokens"]:
            seen.add(key)
            total_in += usage["input_tokens"]
            total_out += usage["output_tokens"]
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

    # Model clients are async-only (see b788b62); use ainvoke instead of invoke.
    invoked = asyncio.run(agent.ainvoke(payload, config=config))
    state = invoked if isinstance(invoked, dict) else {"messages": invoked}
    _print_tokens_from_state(state)
    return (
        extract_last_ai_text(state),
        False,
        None,
    )


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
    """Run a single coding task headless and exit."""
    env_path = _bootstrap_env()
    settings = _resolve_settings(
        workspace=workspace,
        model=model,
        require_approval=require_approval,
        debug=debug,
        readonly=readonly,
    )
    print_banner(str(settings.workspace), settings.model, settings.require_approval)
    if env_path is not None:
        print_info(f"loaded env: {env_path}")
    print_info(
        f"auth: key={settings.mask_openai_key()} "
        f"base_url={settings.openai_base_url!r} model={settings.model!r}"
    )

    try:
        from synapse.app.agent import build_coding_agent, default_thread_id

        agent = build_coding_agent(
            settings,
            project_root=settings.workspace,
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
        f"stream: token={settings.token_stream} "
        f"parallel_tools={settings.parallel_tool_calls} "
        f"max_concurrency={settings.max_concurrency}"
    )

    try:
        answer, already, streamed = _run_once(
            agent,
            payload,
            config,
            use_stream=stream,
            token_stream=settings.token_stream,
            max_concurrency=settings.max_concurrency,
        )
        if streamed is not None and getattr(streamed, "interrupted", False):
            print_error(
                "task paused for approval; run without --require-approval "
                f"or resume later with thread_id={tid}"
            )
            raise typer.Exit(code=2)
        if not already:
            print_final(answer)
    except typer.Exit:
        raise
    except Exception as exc:  # noqa: BLE001
        _print_auth_error(settings, exc)
        raise typer.Exit(code=1) from exc


# ---------------------------------------------------------------------------
# Sub-commands: sessions
# ---------------------------------------------------------------------------


def _import_codex_session(
    native_id: str,
    *,
    workspace: Path | None = None,
    codex_home: Path | None = None,
):
    """Import one safe Codex visible-text snapshot into a Synapse thread."""
    from synapse.app.agent import build_coding_agent
    from synapse.integrations.codex_import import import_codex_session

    settings = load_settings(workspace=workspace) if workspace is not None else load_settings()
    agent = build_coding_agent(settings, project_root=settings.workspace, load_mcp=False)
    try:
        return import_codex_session(
            native_id=native_id,
            settings=settings,
            agent=agent,
            workspace=workspace,
            codex_home=codex_home,
        )
    except Exception as exc:  # noqa: BLE001
        prefix = "Codex session cannot be imported safely: "
        message = str(exc)
        if message.startswith(prefix):
            codes = message.removeprefix(prefix).split(",")
            reasons = ", ".join(_preview_warning_text(code) for code in codes)
            raise ValueError(f"Codex session cannot be imported safely: {reasons}") from exc
        raise ValueError(message) from exc


@sessions_app.command("list")
def sessions_list(
    limit: int = typer.Option(50, "--limit", "-n", help="Max sessions"),
    all_sessions: bool = typer.Option(
        False,
        "--all",
        help="Include empty placeholder sessions (default: hide them)",
    ),
    all_projects: bool = typer.Option(
        False,
        "--all-projects",
        help="List across every registered project via the global catalog",
    ),
) -> None:
    """List recent sessions."""
    settings = load_settings()
    if all_projects:
        from synapse.projects.catalog import ProjectCatalog

        catalog = ProjectCatalog(settings.resolved_catalog_path())
        items = catalog.list_sessions(limit=limit)
        if not items:
            console.print("No sessions in the global catalog.")
            console.print(
                "Hint: start `synapse` at least once per project to register it, "
                "or run `synapse projects sync`."
            )
            return
        for item in items:
            console.print(
                f"{item.updated_at[:16].replace('T', ' ')}  "
                f"[{item.project_name}] {item.thread_id}  {item.title}"
            )
        return
    store = _session_store(settings)
    items = store.list(limit=limit) if all_sessions else store.list_nonempty(limit=limit)
    console.print(format_session_table(items))


# ---------------------------------------------------------------------------
# Sub-commands: projects (global catalog)
# ---------------------------------------------------------------------------


def _catalog() -> ProjectCatalog:
    return ProjectCatalog(load_settings().resolved_catalog_path())


def _resolve_project_ref(ref: str) -> ProjectInfo | None:
    return _catalog().resolve_project(ref)


@projects_app.command("list")
def projects_list(
    limit: int = typer.Option(50, "--limit", "-n", min=1, max=500, help="Max projects"),
) -> None:
    """List every registered project (most recently active first)."""
    items = _catalog().list_projects(limit=limit)
    if not items:
        console.print("No projects registered yet.")
        console.print("Hint: run `synapse` inside a project to register it automatically.")
        return
    console.print(f"{'project':<24} {'sessions':>8} {'runs':>5}  {'last active':<16} path")
    for item in items:
        console.print(
            f"{item.project_id[:12]:<24} {item.session_count:>8} {item.run_count:>5}  "
            f"{item.last_active_at[:16].replace('T', ' '):<16} {item.workspace_path}"
        )


@projects_app.command("show")
def projects_show(ref: str = typer.Argument(..., help="Project id prefix, name, or path")) -> None:
    """Show one project and its recent sessions."""
    project = _resolve_project_ref(ref)
    if project is None:
        print_error(f"project not found: {ref}")
        raise typer.Exit(code=1)
    console.print(f"project_id:  {project.project_id}")
    console.print(f"name:        {project.name}")
    console.print(f"workspace:   {project.workspace_path}")
    console.print(f"git_remote:  {project.git_remote or '-'}")
    console.print(f"git_branch:  {project.git_branch or '-'}")
    console.print(f"created:     {project.created_at}")
    console.print(f"last active: {project.last_active_at}")
    console.print(f"sessions:    {project.session_count}")
    console.print(f"runs:        {project.run_count}")
    sessions = _catalog().list_sessions(project_id=project.project_id, limit=10)
    if sessions:
        console.print("\nrecent sessions:")
        for item in sessions:
            console.print(
                f"  {item.updated_at[:16].replace('T', ' ')}  {item.thread_id}  {item.title}"
            )


@projects_app.command("sessions")
def projects_sessions(
    ref: str = typer.Argument(..., help="Project id prefix, name, or path"),
    limit: int = typer.Option(100, "--limit", "-n", min=1, max=1000, help="Max sessions"),
    search: str | None = typer.Option(None, "--search", help="Filter by title/summary text"),
) -> None:
    """List sessions of one project (from the global catalog projection)."""
    project = _resolve_project_ref(ref)
    if project is None:
        print_error(f"project not found: {ref}")
        raise typer.Exit(code=1)
    catalog = _catalog()
    items = (
        catalog.search_sessions(search, workspace=project.workspace_path, limit=limit)
        if search
        else catalog.list_sessions(workspace=project.workspace_path, limit=limit)
    )
    if not items:
        console.print(f"No sessions for project {project.name}.")
        return
    for item in items:
        line = (
            f"{item.updated_at[:16].replace('T', ' ')}  {item.thread_id}  {item.title}"
        )
        if item.summary:
            line += f"\n    {item.summary}"
        console.print(line)


@projects_app.command("search")
def projects_search(
    query: str = typer.Argument(..., help="Text to match against session titles/summaries"),
    limit: int = typer.Option(50, "--limit", "-n", min=1, max=500, help="Max sessions"),
) -> None:
    """Search sessions across all registered projects."""
    items = _catalog().search_sessions(query, limit=limit)
    if not items:
        console.print(f"No sessions match {query!r}.")
        return
    console.print(f"{len(items)} session(s) match {query!r}:")
    for item in items:
        console.print(
            f"{item.updated_at[:16].replace('T', ' ')}  [{item.project_name}] "
            f"{item.thread_id}  {item.title}"
        )
        if item.summary:
            console.print(f"    {item.summary}")


@projects_app.command("sync")
def projects_sync(
    workspace: Path | None = typer.Option(
        None, "--workspace", "-w", help="Project workspace (default: cwd)"
    ),
) -> None:
    """Reconcile the current (or given) project's sessions into the catalog."""
    settings = (
        load_settings(workspace=workspace) if workspace is not None else load_settings()
    )
    catalog = ProjectCatalog(settings.resolved_catalog_path())
    project = catalog.register_project(settings.workspace)
    count = catalog.sync_project(settings)
    console.print(
        f"project {project.name} ({project.project_id[:12]}): "
        f"{count} session(s) projected."
    )


@projects_app.command("runs")
def projects_runs(
    ref: str | None = typer.Option(None, "--project", help="Project id prefix, name, or path"),
    limit: int = typer.Option(50, "--limit", "-n", min=1, max=500, help="Max runs"),
) -> None:
    """List recorded launches (TUI/CLI) per project."""
    catalog = _catalog()
    project = _resolve_project_ref(ref) if ref else None
    items = catalog.list_runs(
        project_id=project.project_id if project is not None else None, limit=limit
    )
    if not items:
        console.print("No recorded runs.")
        return
    for run in items:
        project_info = catalog.get_project(project_id=run.project_id)
        name = project_info.name if project_info is not None else run.project_id[:12]
        status = "running" if run.finished_at is None else f"exit={run.exit_code}"
        console.print(
            f"{run.started_at[:16].replace('T', ' ')}  [{name}] {run.mode}  "
            f"{status}  thread={run.thread_id or '-'}"
        )


@projects_app.command("stats")
def projects_stats() -> None:
    """Aggregate activity counts across the catalog."""
    stats = _catalog().stats()
    console.print(f"projects:       {stats['projects']}")
    console.print(f"projected sessions: {stats['sessions']}")
    console.print(f"recorded runs:  {stats['runs']}")
    console.print(f"active today:   {stats['active_today']}")


@sessions_app.command("codex-list")
def sessions_codex_list(
    workspace: Path | None = typer.Option(
        None, "--workspace", "-w", help="Filter Codex sessions to one workspace"
    ),
    codex_home: Path | None = typer.Option(
        None, "--codex-home", help="Codex home directory (default: CODEX_HOME or ~/.codex)"
    ),
    limit: int = typer.Option(50, "--limit", "-n", min=1, max=200, help="Max sessions"),
) -> None:
    """List read-only Codex session metadata, optionally for one workspace."""
    from synapse.integrations.codex_sessions import CodexSessionScanner

    result = CodexSessionScanner(codex_home).scan(workspace, limit=limit)
    scope = str(workspace.resolve()) if workspace is not None else "all workspaces"
    if not result.sessions:
        print_info(f"no Codex sessions found for {scope}")
    else:
        for session in result.sessions:
            print_info(
                f"{session.native_id}  {session.updated_at:%Y-%m-%d %H:%M}  "
                f"{session.source:8s}  {session.cwd}  {session.title}"
            )
    for warning in result.warnings:
        print_info(f"warning: {warning}")


@sessions_app.command("codex-inspect")
def sessions_codex_inspect(
    native_id: str = typer.Argument(..., help="Codex native session id"),
    workspace: Path | None = typer.Option(
        None, "--workspace", "-w", help="Filter Codex sessions to one workspace"
    ),
    codex_home: Path | None = typer.Option(
        None, "--codex-home", help="Codex home directory (default: CODEX_HOME or ~/.codex)"
    ),
) -> None:
    """Show read-only metadata for one Codex session."""
    from synapse.integrations.codex_sessions import CodexSessionScanner

    scanner = CodexSessionScanner(codex_home)
    session = scanner.inspect(native_id, workspace=workspace)
    if session is None:
        print_error(f"Codex session not found: {native_id}")
        raise typer.Exit(code=1)
    for key, value in session.to_dict().items():
        if key == "warnings":
            continue
        print_info(f"{key}: {value}")
    for warning in session.warnings:
        print_info(f"warning: {warning}")


MAX_PREVIEW_MESSAGE_CHARS = 12_000


_PREVIEW_WARNING_TEXT = {
    "internal_user_message": "历史包含内部提示内容",
    "invalid_json": "历史文件不是有效的 JSONL",
    "legacy_compaction_unsupported": "历史使用了暂不支持的旧版压缩格式",
    "no_visible_messages": "历史没有可导入的已完成用户或助手消息",
    "rollout_line_limit": "历史中有超过安全上限的单行内容",
    "rollout_not_utf8": "历史文件不是 UTF-8 文本",
    "rollout_read_failed": "历史文件无法读取",
    "rollout_size_limit": "历史解压后的大小超过安全上限",
    "rollout_zstd_invalid": "压缩的历史文件已损坏或不是有效 zstd 数据",
    "unsupported_replacement_content": "压缩后的历史包含暂不支持的内容",
    "unsupported_replacement_item": "压缩后的历史包含暂不支持的记录",
}


def _preview_warning_text(code: str) -> str:
    return _PREVIEW_WARNING_TEXT.get(code, "历史包含暂不支持的记录")


def _bounded_preview_text(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_PREVIEW_MESSAGE_CHARS:
        return text, False
    return text[:MAX_PREVIEW_MESSAGE_CHARS] + "\n[message truncated]", True


@sessions_app.command("codex-preview")
def sessions_codex_preview(
    native_id: str = typer.Argument(..., help="Codex native session id"),
    workspace: Path | None = typer.Option(
        None, "--workspace", "-w", help="Filter Codex sessions to one workspace"
    ),
    codex_home: Path | None = typer.Option(
        None, "--codex-home", help="Codex home directory (default: CODEX_HOME or ~/.codex)"
    ),
    limit: int = typer.Option(100, "--limit", "-n", min=1, max=500, help="Max visible messages"),
    offset: int = typer.Option(0, "--offset", min=0, help="Visible message offset"),
) -> None:
    """Preview safe, completed user and assistant text from one Codex session."""
    from synapse.integrations.codex_history import CodexHistoryProjector
    from synapse.integrations.codex_sessions import CodexSessionScanner

    session = CodexSessionScanner(codex_home).inspect(native_id, workspace=workspace)
    if session is None:
        print_error(f"Codex session not found: {native_id}")
        raise typer.Exit(code=1)

    snapshot = CodexHistoryProjector().project_path(session.rollout_path)
    if not snapshot.importable:
        print_error("Codex session cannot be previewed safely")
        for warning in snapshot.warnings:
            print_info(f"reason: {_preview_warning_text(warning.code)}")
        raise typer.Exit(code=1)

    page = snapshot.messages[offset : offset + limit]
    print_info(f"Codex session: {session.title}")
    print_info(f"Workspace: {session.cwd}")
    print_info(f"Showing messages {offset + 1}-{offset + len(page)} of {len(snapshot.messages)}")
    message_was_truncated = False
    for message in page:
        label = "User" if message.role == "user" else "Assistant"
        text, truncated = _bounded_preview_text(message.text)
        message_was_truncated = message_was_truncated or truncated
        console.print(f"\n[{label}]\n{text}")
    if offset + len(page) < len(snapshot.messages):
        print_info(f"more messages: use --offset {offset + len(page)}")
    if message_was_truncated:
        print_info(f"messages longer than {MAX_PREVIEW_MESSAGE_CHARS} characters were truncated")
    for warning in snapshot.warnings:
        print_info(f"warning: {_preview_warning_text(warning.code)}")


@sessions_app.command("codex-import")
def sessions_codex_import(
    native_id: str = typer.Argument(..., help="Codex native session id"),
    workspace: Path | None = typer.Option(
        None, "--workspace", "-w", help="Filter Codex sessions to one workspace"
    ),
    codex_home: Path | None = typer.Option(
        None, "--codex-home", help="Codex home directory (default: CODEX_HOME or ~/.codex)"
    ),
) -> None:
    """Import one safe Codex visible-text snapshot into a new Synapse session."""
    try:
        result = _import_codex_session(native_id, workspace=workspace, codex_home=codex_home)
    except Exception as exc:  # noqa: BLE001
        print_error(str(exc))
        raise typer.Exit(code=1) from exc
    status = "reused" if result.reused else "recovered" if result.recovered else "imported"
    print_info(f"Codex session {status}: thread_id={result.thread_id}")


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

    from synapse.sessions.transcript import (
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
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in (thread_id or "session"))
        out = settings.export_dir() / f"{safe}.{fmt_n}"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print_info(f"exported -> {out}")


@sessions_app.command("search")
def sessions_search(
    query: str = typer.Argument(..., help="Keywords / sub-string"),
    limit: int = typer.Option(20, "--limit", "-n", help="Max results"),
) -> None:
    """Search session titles / summaries."""
    settings = load_settings()
    store = _session_store(settings)
    hits = store.search(query, limit=limit)
    console.print(format_session_table(hits))


# ---------------------------------------------------------------------------
# Sub-commands: models
# ---------------------------------------------------------------------------


@models_app.command("list")
def models_list() -> None:
    """List configured downstream model profiles."""
    from synapse.models.registry import registry_from_settings

    settings = load_settings()
    reg = registry_from_settings(settings)
    profiles = reg.list_profiles()
    if not profiles:
        print_info("No model profiles configured. Edit models.json inside .coding-agent/")
        return
    for pf in profiles:
        alias = pf.name
        if alias == reg.default:
            alias += "  * (default)"
        print_info(f"  {alias:30s} provider={pf.provider:20s} model={pf.model:20s}")
        if pf.thinking:
            print_info(
                f"    thinking: budget={pf.thinking.get('budget')} type={pf.thinking.get('type')}"
            )
        if pf.profile_arg:
            print_info(f"    extra_body: profile={pf.profile_arg}")
        if pf.max_tokens:
            print_info(f"    max_tokens={pf.max_tokens}")


# ---------------------------------------------------------------------------
# Sub-commands: mcp
# ---------------------------------------------------------------------------


@mcp_app.command("list")
def mcp_list() -> None:
    """List configured MCP servers."""
    from synapse.integrations.mcp_client import load_mcp_server_configs

    settings = load_settings()
    servers = load_mcp_server_configs(settings)
    if not servers:
        print_info("No MCP servers configured.")
        return
    for name, cfg in servers.items():
        transport = cfg.get("transport", "http")
        print_info(f"  {name:25s} transport={transport}")


@mcp_app.command("test")
def mcp_test(
    server: str = typer.Argument(..., help="MCP server name"),
) -> None:
    """Connect to an MCP server and print available tools."""

    from synapse.integrations.mcp_client import connect_mcp, load_mcp_server_configs

    settings = load_settings()
    configs = load_mcp_server_configs(settings)
    if server not in configs:
        print_error(f"MCP server not configured: {server}")
        raise typer.Exit(code=1)

    async def _connect():
        return await connect_mcp(server, configs[server])

    try:
        session = asyncio.run(_connect())
    except Exception as exc:  # noqa: BLE001
        print_error(f"failed to connect to MCP server '{server}': {exc}")
        raise typer.Exit(code=1) from exc

    tools = session.get_tools()
    if not tools:
        print_info(f"No tools from MCP server '{server}'")
    else:
        print_info(f"MCP server '{server}' — {len(tools)} tool(s):")
        for t in tools:
            desc = getattr(t, "description", "") or ""
            print_info(f"  {t.name:30s} {desc}")


@tool_output_app.command("eval")
def tool_output_eval(
    fixture: Path = typer.Argument(
        ..., exists=True, readable=True, help="JSON array of offline eval cases"
    ),
) -> None:
    """Evaluate deterministic retention and compression against fixed fixtures."""
    from synapse.runtime.tool_output_eval import evaluate_cases, load_cases, summarize_results

    summary = summarize_results(evaluate_cases(load_cases(fixture)))
    console.print(f"cases: {summary['cases']}; passed: {summary['passed']}")
    console.print(f"savings: {summary['savings_ratio']:.1%}")
    console.print(f"required retention: {summary['required_retention']:.1%}")
    for result in summary["results"]:
        console.print(
            f"- {result['id']}: {result['type']} via {result['transformer']}; "
            f"savings={result['savings_ratio']:.1%}; passed={result['passed']}"
        )
    if summary["passed"] != summary["cases"]:
        raise typer.Exit(code=1)


@tool_output_app.command("stats")
def tool_output_stats(
    thread_id: str | None = typer.Option(None, "--thread", help="Restrict metrics to a thread id"),
) -> None:
    """Show local tool-output transformation savings and retention metrics."""
    from synapse.tool_output.repository import ToolOutputRepository

    settings = load_settings()
    stats = ToolOutputRepository(settings.resolved_tool_output_db_path()).stats(thread_id=thread_id)
    console.print("Tool output transformation")
    console.print(f"outputs considered: {stats['outputs_considered']}")
    console.print(f"transformed: {stats['transformed']}")
    console.print(f"original bytes: {stats['original_bytes']}")
    console.print(f"visible bytes: {stats['visible_bytes']}")
    console.print(f"saved bytes: {stats['saved_bytes']} ({stats['savings_ratio']:.1%})")
    console.print(f"retrieval bytes: {stats['retrieval_bytes']}")
    console.print(
        f"effective saved bytes: {stats['effective_saved_bytes']} "
        f"({stats['effective_savings_ratio']:.1%})"
    )
    console.print(f"critical retention: {stats['critical_retention']:.1%}")
    paths = stats["execution_paths"]
    if paths:
        console.print(
            "execution paths: "
            + ", ".join(f"{name}={count}" for name, count in sorted(paths.items()))
        )


@tool_output_app.command("status")
def tool_output_status() -> None:
    """Show whether tool-output transformation and native acceleration are usable."""
    from synapse.tool_output.transformers import load_native_transformers

    settings = load_settings()
    native_requested = settings.enable_native_tool_output_compression
    native_transformers = load_native_transformers(enabled=native_requested)
    console.print("Tool output transformation status")
    console.print(f"transform enabled: {settings.enable_tool_output_transform}")
    console.print(f"threshold bytes: {settings.tool_output_transform_threshold_bytes}")
    console.print(f"database: {settings.resolved_tool_output_db_path()}")
    console.print(f"native enabled by config: {native_requested}")
    console.print(f"native wheel loadable: {bool(native_transformers)}")
    console.print(
        "active native types: "
        + (
            ", ".join(sorted(next(iter(item.content_types)).value for item in native_transformers))
            if native_transformers
            else "none"
        )
    )


@tool_output_app.command("events")
def tool_output_events(
    thread_id: str | None = typer.Option(None, "--thread", help="Restrict events to a thread id"),
    limit: int = typer.Option(50, "--limit", "-n", min=1, max=500, help="Max recent events"),
) -> None:
    """Show recent transformation decisions and retrieval usage."""
    from synapse.tool_output.repository import ToolOutputRepository

    settings = load_settings()
    events = ToolOutputRepository(settings.resolved_tool_output_db_path()).events(
        thread_id=thread_id, limit=limit
    )
    if not events:
        console.print("No tool-output events.")
        return
    console.print("Tool output transformation events")
    for event in events:
        saved = int(event["saved_bytes"])
        console.print(
            f"{event['created_at']}  thread={event['thread_id']}  "
            f"type={event['content_type']}  transformer={event['transformer']}\n"
            f"  outcome={event['outcome']}  path={event.get('execution_path', 'unknown')}  "
            f"original={event['original_bytes']}  visible={event['visible_bytes']}  "
            f"saved={saved}  retrieved={event['retrieval_bytes']}  "
            f"critical={event['critical_retained']}/{event['critical_total']}\n"
            f"  ref={event['ref'] or '-'}"
        )


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


@app.command("version")
def version_cmd() -> None:
    """Print package version."""
    from synapse import __version__

    console.print(__version__)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Console script entrypoint."""
    os.environ.setdefault("PYTHONUTF8", "1")
    try:
        from synapse.observability.startup_trace import ensure_started, mark

        ensure_started()
        mark("cli:main")
    except Exception:  # noqa: BLE001
        pass
    try:
        from synapse.observability.heap_dump import start_heap_dump_watchdog

        start_heap_dump_watchdog()
    except Exception:  # noqa: BLE001
        pass
    app()


if __name__ == "__main__":
    main()