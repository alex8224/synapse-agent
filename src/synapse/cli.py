"""Typer CLI for the local coding agent."""

from __future__ import annotations

import os
from pathlib import Path

import typer

from synapse.config import bootstrap_project_env, load_settings
from synapse.sessions import SessionStore, format_session_table
from synapse.ui.stream import (
    console,
    print_error,
    print_info,
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
app.add_typer(sessions_app, name="sessions")
app.add_typer(models_app, name="models")
app.add_typer(mcp_app, name="mcp")


# ---------------------------------------------------------------------------
# Common helpers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Default callback: launch TUI when no subcommand is given
# ---------------------------------------------------------------------------


@app.callback(invoke_without_command=True)
def _default_tui(
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
    """Full-screen Textual TUI – the default interface."""
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
    from synapse.agent import build_coding_agent
    from synapse.codex_import import import_codex_session

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
) -> None:
    """List recent sessions."""
    settings = load_settings()
    store = _session_store(settings)
    items = store.list(limit=limit) if all_sessions else store.list_nonempty(limit=limit)
    console.print(format_session_table(items))


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
    from synapse.codex_sessions import CodexSessionScanner

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
    from synapse.codex_sessions import CodexSessionScanner

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
    from synapse.codex_history import CodexHistoryProjector
    from synapse.codex_sessions import CodexSessionScanner

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
    from synapse.models_registry import registry_from_settings

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
                f"    thinking: budget={pf.thinking.get('budget')} "
                f"type={pf.thinking.get('type')}"
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
    from synapse.mcp_client import load_mcp_server_configs

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
    import asyncio

    from synapse.mcp_client import connect_mcp, load_mcp_server_configs

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
        from synapse.startup_trace import ensure_started, mark

        ensure_started()
        mark("cli:main")
    except Exception:  # noqa: BLE001
        pass
    app()


if __name__ == "__main__":
    main()
