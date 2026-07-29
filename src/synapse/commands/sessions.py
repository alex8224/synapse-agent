"""Session lifecycle and transcript export slash-command handler."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from synapse.commands.helpers import markdown_escape
from synapse.commands.result import SlashResult
from synapse.sessions.store import (
    SessionStore,
    allocate_thread_id,
    binding_from_settings,
    format_session_table,
)
from synapse.sessions.transcript import (
    export_transcript_json,
    export_transcript_markdown,
    load_thread_messages,
)


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


def _md_session_show(store: SessionStore, thread_id: str, settings: Any) -> str:
    """Format current session info as a Markdown table."""
    info = store.get(thread_id) or store.ensure(
        thread_id,
        model=_model_name(settings),
        active_model=getattr(settings, "active_model", None),
        thinking=binding_from_settings(settings).thinking,
    )
    bind = info.binding()
    rows = [
        ("thread_id", f"`{info.thread_id}`"),
        ("title", info.title),
        ("model", bind.display()),
        ("active_model", info.active_model or "-"),
        ("thinking", info.thinking or "-"),
        ("created", info.created_at),
        ("updated", info.updated_at),
        ("tags", ", ".join(info.tags) if info.tags else "-"),
    ]
    lines = ["## Current Session", "", "| Property | Value |", "|---|---|"]
    for k, v in rows:
        lines.append(f"| {markdown_escape(k)} | {markdown_escape(v)} |")
    return "\n".join(lines)


def _md_session_table(items: list[Any]) -> str:
    """Format session list as a Markdown table."""
    if not items:
        return "*No sessions found*"
    lines = [f"## Sessions ({len(items)})", ""]
    lines.append("| ID | Title | Model | Updated |")
    lines.append("|---|---|---|---|")
    for s in items:
        tid = s.thread_id[:12]
        title = (s.title or "-")[:48]
        model = (s.binding().display() or "-")[:20]
        updated = s.updated_at or "-"
        lines.append(
            f"| `{markdown_escape(tid)}` | {markdown_escape(title)} "
            f"| {markdown_escape(model)} | {markdown_escape(updated)} |"
        )
    return "\n".join(lines)
def _load_messages(agent: Any, settings: Any, thread_id: str) -> list[Any]:
    return load_thread_messages(agent=agent, settings=settings, thread_id=thread_id)


def _normalize_export_fmt(fmt: str | None) -> str:
    raw = (fmt or "md").strip().lower()
    if raw in {"json", "j"}:
        return "json"
    return "md"


def _default_export_path(settings: Any, thread_id: str, fmt: str) -> Path:
    """Default export location next to session/checkpoint state."""
    ext = "json" if fmt == "json" else "md"
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in (thread_id or "session"))
    safe = (safe or "session")[:80]
    parent = Path(settings.checkpoint_path).expanduser().resolve().parent
    return parent / "exports" / f"{safe}.{ext}"


def _export_lines(
    *,
    settings: Any,
    agent: Any,
    thread_id: str,
    fmt: str,
    out_path: Path | None,
) -> SlashResult:
    """Export transcript to a file only (never dump body into TUI/chat log)."""
    store = _store(settings)
    model = _model_name(settings)
    info = store.get(thread_id) or store.ensure(thread_id, model=model)
    messages = _load_messages(agent, settings, thread_id)
    fmt_n = _normalize_export_fmt(fmt)

    if fmt_n == "json":
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

    target = out_path if out_path is not None else _default_export_path(settings, thread_id, fmt_n)
    try:
        target = Path(target).expanduser()
        if not target.is_absolute():
            target = (Path.cwd() / target).resolve()
        else:
            target = target.resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    except OSError as exc:
        return SlashResult(
            handled=True,
            lines=[f"export failed: {exc}"],
            error=True,
        )

    confirm = f"exported {fmt_n} -> {target}"
    return SlashResult(
        handled=True,
        lines=[confirm, f"messages: {len(messages)}"],
        notice=confirm,
        markdown=(
            "## Export\n\n"
            f"- **format**: {fmt_n}\n"
            f"- **path**: `{target}`\n"
            f"- **messages**: {len(messages)}\n"
        ),
    )


def handle_session(
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
            return SlashResult(
                handled=True,
                lines=_session_show(store, thread_id, settings),
                markdown=_md_session_show(store, thread_id, settings),
            )
        return SlashResult(
            handled=True,
            lines=[format_session_table(store.list_nonempty())],
            markdown=_md_session_table(store.list_nonempty()),
        )

    if cmd == "/sessions" and args:
        sub = args[0].lower()
        rest = args[1:]
        return handle_session(
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
            markdown=(
                "## New Session\n\n"
                f"- **thread_id**: `{tid}`\n"
                f"- **model**: {bind.display()}\n\n"
                "*Session metadata is saved on the first message.*"
            ),
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
                        "tip: /sessions 鈥?list titles; match must be unique",
                    ],
                    error=True,
                )
            store.ensure(tid, model=model)
            return SlashResult(
                handled=True,
                lines=[f"switched thread_id={tid}"],
                markdown=f"## Switched\n\n- **thread_id**: `{tid}`",
                thread_id=tid,
                settings_changed=True,
                clear_log=True,
                reload_transcript=True,
            )
        return SlashResult(
            handled=True,
            lines=[f"switched thread_id={info.thread_id}  title={info.title}"],
            markdown=(
                "## Switched\n\n"
                f"- **thread_id**: `{info.thread_id}`\n"
                f"- **title**: {markdown_escape(info.title)}"
            ),
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
        new_title = info.title if info else title
        return SlashResult(
            handled=True,
            lines=[f"renamed to: {new_title}"],
            markdown=f"## Renamed\n\n- **title**: {markdown_escape(new_title)}",
        )

    if cmd == "/export":
        fmt = "md"
        out_path: Path | None = None
        if args:
            first = args[0].lower()
            if first in {"md", "markdown", "m", "json", "j"}:
                fmt = "json" if first in {"json", "j"} else "md"
                if len(args) >= 2:
                    out_path = Path(" ".join(args[1:])).expanduser()
            else:
                # /export path/to/file.md  (format inferred from suffix)
                out_path = Path(" ".join(args)).expanduser()
                suffix = out_path.suffix.lower()
                if suffix == ".json":
                    fmt = "json"
                else:
                    fmt = "md"
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
        return SlashResult(
            handled=True,
            lines=_session_show(store, thread_id, settings),
            markdown=_md_session_show(store, thread_id, settings),
        )

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
        sessions = store.list_nonempty(limit=limit)
        return SlashResult(
            handled=True,
            lines=[format_session_table(sessions)],
            markdown=_md_session_table(sessions),
        )

    if sub == "prune":
        deleted = store.prune_empty(except_ids={thread_id} if thread_id else set())
        lines = [f"pruned {len(deleted)} empty session(s)"]
        lines.extend(f"  - {tid}" for tid in deleted[:20])
        if len(deleted) > 20:
            lines.append(f"  鈥?and {len(deleted) - 20} more")
        md = f"## Pruned\n\n**{len(deleted)}** empty session(s) removed.\n"
        if deleted:
            md += "\n" + "\n".join(f"- `{tid}`" for tid in deleted[:20])
            if len(deleted) > 20:
                md += f"\n- *鈥?and {len(deleted) - 20} more*"
        return SlashResult(handled=True, lines=lines, markdown=md)

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
        return SlashResult(
            handled=True,
            lines=_session_show(store, tid, settings),
            markdown=_md_session_show(store, tid, settings),
        )

    if sub == "new":
        return handle_session("/new", [], settings=settings, agent=agent, thread_id=thread_id)

    if sub == "switch":
        return handle_session("/switch", rest, settings=settings, agent=agent, thread_id=thread_id)

    if sub == "rename":
        return handle_session("/rename", rest, settings=settings, agent=agent, thread_id=thread_id)

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
                markdown=(
                    f"## Deleted\n\n- **thread_id**: `{tid}`\n- **title**: {markdown_escape(label)}"
                ),
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
        results = store.search(q)
        return SlashResult(
            handled=True,
            lines=[format_session_table(results)],
            markdown=_md_session_table(results),
        )

    if sub == "export":
        return handle_session("/export", rest, settings=settings, agent=agent, thread_id=thread_id)

    return SlashResult(
        handled=True,
        lines=[
            "usage: /session [list|show|new|switch|rename|delete|search|export]",
            "also: /sessions /new /switch /rename /export",
        ],
        error=True,
    )
