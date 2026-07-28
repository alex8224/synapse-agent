"""Shared interactive slash commands for chat CLI and TUI.

Focus: session management + MCP management first.
Returns structured results so UIs only need to render/apply side effects.
"""

from __future__ import annotations

import csv
import io
import json
import time
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

### Appearance
| Command | Description |
|---|---|
| `/theme` | Open theme picker (TUI) |
| `/theme list` | List themes |
| `/theme <name>` | Apply theme |

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
    # TUI should attach MCP after applying the rebuilt agent, without blocking
    # the slash command/model switch worker.
    # Rich Markdown text rendered directly into the transcript (#log).  When set,
    # the TUI renders it as a Markdown block instead of plain lines.
    markdown: str | None = None
    mcp_attach_pending: bool = False
    # UI theme switch (TUI should re-apply CSS / palette).
    theme_name: str | None = None
    # HITL: UI should resume the paused graph with this decision.
    resume_action: str | None = None  # "approve" | "reject"
    resume_message: str | None = None


def _parts(text: str) -> list[str]:
    return text.strip().split()


def _format_bytes(value: int | float) -> str:
    """Format byte counts compactly for slash command tables."""
    amount = max(0, int(value or 0))
    for unit, size in (("G", 1024**3), ("M", 1024**2), ("K", 1024)):
        if amount >= size:
            rendered = amount / size
            return f"{rendered:.1f}{unit}" if rendered < 10 else f"{rendered:.0f}{unit}"
    return f"{amount}B"


def _resolve_session_ref(
    settings: Any, current_thread_id: str, args: list[str]
) -> tuple[str | None, str | None]:
    """Resolve an optional thread id/title argument to a unique session id."""
    if not args:
        return current_thread_id, None
    query = " ".join(args).strip()
    info = _store(settings).resolve_session_ref(query)
    if info is None:
        return None, f"session not found or ambiguous: {query}"
    return info.thread_id, None


_COMPRESSION_EXPORT_FORMAT_ALIASES = {
    "json": "json",
    "j": "json",
    "csv": "csv",
    "c": "csv",
}


def _default_compression_export_path(settings: Any, thread_id: str, fmt: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in (thread_id or "session"))
    safe = (safe or "session")[:80]
    parent = Path(settings.checkpoint_path).expanduser().resolve().parent
    return parent / "exports" / f"{safe}.compression.{fmt}"


def _compression_export_csv(payload: dict[str, Any]) -> str:
    """Flatten heterogeneous diagnostics into one portable CSV table."""
    rows: list[dict[str, Any]] = [
        {
            "record_type": "metadata",
            "thread_id": payload["thread_id"],
            "metric": "schema_version",
            "value": payload["schema_version"],
        },
        {
            "record_type": "metadata",
            "thread_id": payload["thread_id"],
            "metric": "exported_at",
            "value": payload["exported_at"],
        },
    ]
    rows.extend(
        {
            "record_type": "summary",
            "thread_id": payload["thread_id"],
            "metric": key,
            "value": value,
        }
        for key, value in sorted(dict(payload.get("summary") or {}).items())
    )
    for record_type, key in (
        ("model_request", "model_request_events"),
        ("interaction", "interaction_events"),
        ("tool_output", "tool_output_events"),
        ("retrieval", "retrieval_events"),
        ("model_reuse", "model_reuse_events"),
    ):
        rows.extend(
            {"record_type": record_type, **dict(item)} for item in payload.get(key, [])
        )

    preferred = [
        "record_type",
        "thread_id",
        "id",
        "created_at",
        "metric",
        "value",
        "request_id",
        "provider",
        "api_style",
        "auth_mode",
        "model",
        "tool_call_id",
        "tool_name",
        "decision",
        "reason_code",
    ]
    all_fields = {key for row in rows for key in row}
    fieldnames = [key for key in preferred if key in all_fields]
    fieldnames.extend(sorted(all_fields - set(fieldnames)))

    def cell(value: Any) -> Any:
        if isinstance(value, dict | list | tuple):
            return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        return "" if value is None else value

    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows({key: cell(value) for key, value in row.items()} for row in rows)
    return output.getvalue()


def _compression_export_result(
    settings: Any, current_thread_id: str, args: list[str]
) -> SlashResult:
    """Export complete compression diagnostics for one session to JSON or CSV."""
    fmt = "json"
    session_args: list[str] = []
    path_args: list[str] = []
    format_index = next(
        (
            index
            for index, value in enumerate(args)
            if value.casefold() in _COMPRESSION_EXPORT_FORMAT_ALIASES
        ),
        None,
    )
    if format_index is not None:
        fmt = _COMPRESSION_EXPORT_FORMAT_ALIASES[args[format_index].casefold()]
        session_args = args[:format_index]
        path_args = args[format_index + 1 :]
    elif args:
        candidate = Path(" ".join(args)).expanduser()
        if candidate.suffix.casefold() in {".json", ".csv"}:
            fmt = candidate.suffix.casefold().lstrip(".")
            path_args = args
        else:
            session_args = args

    thread_id, error = _resolve_session_ref(settings, current_thread_id, session_args)
    if error or thread_id is None:
        return SlashResult(handled=True, lines=[error or "session not found"], error=True)

    from synapse.tool_output import ToolOutputRepository

    repo = ToolOutputRepository(settings.resolved_tool_output_db_path())
    diagnostics = repo.export_diagnostics(thread_id=thread_id)
    payload = {
        "schema_version": 2,
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "thread_id": thread_id,
        **diagnostics,
    }
    text = (
        json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        if fmt == "json"
        else _compression_export_csv(payload)
    )
    target = (
        Path(" ".join(path_args)).expanduser()
        if path_args
        else _default_compression_export_path(settings, thread_id, fmt)
    )
    try:
        target = (Path.cwd() / target).resolve() if not target.is_absolute() else target.resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    except OSError as exc:
        return SlashResult(
            handled=True,
            lines=[f"compression export failed: {exc}"],
            error=True,
        )

    counts = {
        "model requests": len(payload["model_request_events"]),
        "tool calls": len(payload["interaction_events"]),
        "tool outputs": len(payload["tool_output_events"]),
        "retrievals": len(payload["retrieval_events"]),
        "model reuses": len(payload["model_reuse_events"]),
    }
    confirm = f"exported compression {fmt} -> {target}"
    return SlashResult(
        handled=True,
        lines=[
            confirm,
            f"thread_id={thread_id}",
            ", ".join(f"{key}={value}" for key, value in counts.items()),
        ],
        notice=confirm,
        markdown=(
            "## Compression Export\n\n"
            f"- **thread**: `{thread_id}`\n"
            f"- **format**: {fmt}\n"
            f"- **path**: `{target}`\n"
            + "".join(f"- **{key}**: {value}\n" for key, value in counts.items())
        ),
    )


def _tool_output_result(settings: Any, current_thread_id: str, args: list[str]) -> SlashResult:
    """Render persistent compression diagnostics or recent decision events."""
    mode = args[0].casefold() if args else "stats"
    if mode == "export":
        return _compression_export_result(settings, current_thread_id, args[1:])
    show_profile = mode in {"profile", "report"}
    show_requests = mode in {"requests", "request"}
    show_events = mode in {"events", "skipped", "fallback", "tool"}
    rest = args[1:] if show_events or show_requests or show_profile else args
    decision_filter = mode if mode in {"skipped", "fallback"} else ""
    tool_filter = ""
    request_filter = ""
    if mode == "request":
        if not rest:
            return SlashResult(
                handled=True,
                lines=["usage: /compression request <request_id> [session]"],
                error=True,
            )
        request_filter = rest[0]
        rest = rest[1:]
    if mode == "tool":
        if not rest:
            return SlashResult(
                handled=True,
                lines=["usage: /compression tool <tool_call_id> [session] [limit]"],
                error=True,
            )
        tool_filter = rest[0]
        rest = rest[1:]
    limit = 10
    if (show_events or show_requests) and rest and rest[-1].isdigit():
        limit = max(1, min(50, int(rest[-1])))
        rest = rest[:-1]
    thread_id, error = _resolve_session_ref(settings, current_thread_id, rest)
    if error or thread_id is None:
        return SlashResult(handled=True, lines=[error or "session not found"], error=True)

    from synapse.tool_output import ToolOutputRepository

    repo = ToolOutputRepository(settings.resolved_tool_output_db_path())
    if show_profile:
        stats = repo.stats(thread_id=thread_id)
        breakdown = stats.get("content_breakdown") or {}
        opportunities = stats.get("top_opportunities") or []
        md = [
            "## Compression Profile",
            "",
            f"Thread: `{thread_id}`",
            "",
            (
                f"Turns: {stats.get('turns', 0)} · model calls: "
                f"{stats.get('model_requests', 0)} · tool calls: "
                f"{stats.get('tool_calls', 0)} · compression-managed: "
                f"{stats.get('compression_managed_tool_calls', 0)}"
            ),
            (
                f"Cache bust suspected: {stats.get('cache_bust_suspected_requests', 0)} requests"
            ),
            "",
            "### Live-zone distribution",
            "",
            "| Zone | Estimated tokens |",
            "|---|---:|",
            *[
                f"| {_md_escape(str(zone))} | ~{int(tokens or 0)} |"
                for zone, tokens in sorted((stats.get("live_zone_tokens") or {}).items())
            ],
            "",
            "### Tool schema ranking",
            "",
            "| Tool | Cumulative estimated tokens |",
            "|---|---:|",
            *[
                f"| {_md_escape(str(name))} | ~{int(tokens or 0)} |"
                for name, tokens in stats.get("top_schema_tools") or []
            ],
            "",
            "### Request content breakdown",
            "",
            "| Source | Estimated tokens | Share |",
            "|---|---:|---:|",
        ]
        # ``tool_output_original`` reconstructs the pre-compression baseline;
        # it is not part of the final model-visible request. Excluding it keeps
        # model-visible shares additive instead of counting tool output twice.
        reference_sources = {"tool_output_original"}
        total = sum(
            max(0, int(value or 0))
            for source, value in breakdown.items()
            if source not in reference_sources
        )
        lines = [f"thread_id={thread_id}", f"profile_total_tokens=~{total}"]
        for source, tokens in sorted(
            breakdown.items(), key=lambda item: int(item[1] or 0), reverse=True
        ):
            amount = max(0, int(tokens or 0))
            if source in reference_sources:
                md.append(f"| {_md_escape(str(source))} | ~{amount} | — |")
                lines.append(f"{source}=~{amount} (reference; excluded from total)")
                continue
            share = amount / total if total else 0.0
            md.append(f"| {_md_escape(str(source))} | ~{amount} | {share:.1%} |")
            lines.append(f"{source}=~{amount} ({share:.1%})")
        if any(source in breakdown for source in reference_sources):
            md.extend(
                [
                    "",
                    (
                        "`tool_output_original` is the reconstructed pre-compression "
                        "baseline and is excluded from model-visible totals and shares."
                    ),
                ]
            )
        md.extend(
            [
                "",
                "### Ranked optimization opportunities",
                "",
                "| Rank | Reason | Estimated tokens |",
                "|---:|---|---:|",
            ]
        )
        if opportunities:
            for rank, item in enumerate(opportunities, 1):
                reason, tokens = item
                md.append(f"| {rank} | {_md_escape(str(reason))} | ~{int(tokens or 0)} |")
        else:
            md.append("| - | No request profile events yet | 0 |")
        protected = stats.get("top_protected_sources") or []
        md.extend(
            [
                "",
                "### Provider-protected context",
                "",
                "| Reason | Estimated tokens |",
                "|---|---:|",
            ]
        )
        if protected:
            for reason, tokens in protected:
                md.append(f"| {_md_escape(str(reason))} | ~{int(tokens or 0)} |")
        else:
            md.append("| - | 0 |")
        return SlashResult(handled=True, lines=lines, markdown="\n".join(md))
    if show_requests:
        requests = repo.model_request_events(thread_id=thread_id, limit=max(limit, 50))
        if request_filter:
            requests = [item for item in requests if item.get("request_id") == request_filter]
        requests = requests[:limit]
        if not requests:
            return SlashResult(
                handled=True,
                lines=[f"thread_id={thread_id}", "no model request compression events"],
            )
        md = [
            "## Model Request Compression",
            "",
            f"Thread: `{thread_id}`",
            "",
            (
                "| Time | Request | Provider / API | Input before | Input after | "
                "Saved | Cache | Output |"
            ),
            "|---|---|---|---:|---:|---:|---:|---:|",
        ]
        lines = [f"thread_id={thread_id}"]
        for item in requests:
            request_id = str(item.get("request_id") or "-")
            before = int(item.get("input_tokens_before", 0) or 0)
            after = int(item.get("input_tokens_after", 0) or 0)
            saved = int(item.get("total_saved_tokens", 0) or 0)
            cache = int(item.get("cache_read_tokens", 0) or 0)
            output = int(item.get("output_tokens", 0) or 0)
            md.append(
                "| {time} | `{request}` | {provider}/{api} | ~{before} | ~{after} | "
                "~{saved} | {cache} | {output} |".format(
                    time=_md_escape(str(item.get("created_at") or "-")),
                    request=_md_escape(request_id),
                    provider=_md_escape(str(item.get("provider") or "unknown")),
                    api=_md_escape(str(item.get("api_style") or "unknown")),
                    before=before,
                    after=after,
                    saved=saved,
                    cache=cache,
                    output=output,
                )
            )
            protected = item.get("protected_tokens_by_reason") or {}
            breakdown = item.get("content_breakdown") or {}
            opportunities = item.get("opportunity_tokens_by_reason") or {}
            lines.append(
                f"{request_id} turn={item.get('turn_index', 0)} "
                f"call={item.get('model_call_index', 0)} before=~{before} after=~{after} "
                f"saved=~{saved} protected={protected} breakdown={breakdown} "
                f"opportunities={opportunities} live_zone={item.get('live_zone_tokens') or {}} "
                f"cache={item.get('cache_diagnostics') or {}}"
            )
            if request_filter:
                md.extend(
                    [
                        "",
                        "### Content breakdown",
                        "",
                        "| Source | Estimated tokens |",
                        "|---|---:|",
                        *[
                            f"| {_md_escape(str(key))} | ~{int(value or 0)} |"
                            for key, value in sorted(
                                breakdown.items(),
                                key=lambda pair: int(pair[1] or 0),
                                reverse=True,
                            )
                        ],
                        "",
                        "### Optimization opportunities",
                        "",
                        "| Reason | Estimated tokens |",
                        "|---|---:|",
                        *[
                            f"| {_md_escape(str(key))} | ~{int(value or 0)} |"
                            for key, value in sorted(
                                opportunities.items(),
                                key=lambda pair: int(pair[1] or 0),
                                reverse=True,
                            )
                        ],
                    ]
                )
        return SlashResult(handled=True, lines=lines, markdown="\n".join(md))
    if show_events:
        fetch_limit = min(500, max(limit, 50) if decision_filter or tool_filter else limit)
        events = repo.events(thread_id=thread_id, limit=fetch_limit)
        if decision_filter:
            events = [
                event
                for event in events
                if str(event.get("decision") or "").casefold() == decision_filter
            ]
        if tool_filter:
            events = [
                event
                for event in events
                if str(event.get("tool_call_id") or "") == tool_filter
            ]
        events = events[:limit]
        if not events:
            return SlashResult(
                handled=True,
                lines=[f"thread_id={thread_id}", "no tool-output events"],
                markdown=(
                    f"## Tool Output Events\n\nThread: `{thread_id}`\n\n"
                    "No tool-output events."
                ),
            )
        md = [
            "## Compression Decision Events",
            "",
            f"Thread: `{thread_id}`",
            "",
            (
                "| Time | Tool / ID | Type | Decision | Reason | Pipeline | "
                "Original | Final | Saved tok |"
            ),
            "|---|---|---|---|---|---|---:|---:|---:|",
        ]
        lines = [f"thread_id={thread_id}"]
        for event in events:
            saved = int(event.get("estimated_saved_tokens", 0) or 0)
            tool = str(event.get("tool_name") or "-")
            call_id = str(event.get("tool_call_id") or "-")
            decision = str(
                event.get("decision")
                or ("transformed" if event.get("outcome") == "transformed" else "fallback")
            )
            reason = str(event.get("reason_code") or "legacy_passthrough")
            row = (
                "| {time} | {tool}<br>`{call_id}` | {type} | {decision} | {reason} | "
                "{transformer} | {original} | {visible} | {saved} |"
            )
            md.append(
                row.format(
                    time=_md_escape(str(event.get("created_at", "-"))),
                    tool=_md_escape(tool),
                    call_id=_md_escape(call_id),
                    type=_md_escape(str(event.get("content_type", "-"))),
                    decision=_md_escape(decision),
                    reason=_md_escape(reason),
                    transformer=_md_escape(str(event.get("transformer", "-"))),
                    original=_format_bytes(event.get("original_bytes", 0)),
                    visible=_format_bytes(event.get("visible_bytes", 0)),
                    saved=f"~{saved}" if saved else "0",
                )
            )
            lines.append(
                f"{event.get('created_at', '-')} {tool}/{call_id} "
                f"{decision}:{reason} saved_tokens=~{saved}"
            )
        return SlashResult(handled=True, lines=lines, markdown="\n".join(md))

    stats = repo.stats(thread_id=thread_id)
    effective_saved = _format_bytes(stats["effective_saved_bytes"])
    effective_ratio = f"{stats['effective_savings_ratio']:.1%}"
    rows = [
        ("thread_id", thread_id),
        ("outputs considered", str(stats["outputs_considered"])),
        ("transformed", str(stats["transformed"])),
        ("skipped", str(stats.get("skipped", 0) or 0)),
        ("fallback", str(stats.get("fallback", 0) or 0)),
        ("model requests", str(stats.get("model_requests", 0) or 0)),
        (
            "request input before/after",
            f"~{stats.get('request_input_tokens_before', 0)}/"
            f"~{stats.get('request_input_tokens_after', 0)}",
        ),
        ("request saved tokens", f"~{stats.get('request_saved_tokens', 0) or 0}"),
        ("whole request savings", f"{stats.get('whole_request_savings_ratio', 0.0):.1%}"),
        ("new input savings", f"{stats.get('new_input_savings_ratio', 0.0):.1%}"),
        (
            "provider input/cache/output",
            f"{stats.get('provider_input_tokens', 0)}/"
            f"{stats.get('cache_read_tokens', 0)}/"
            f"{stats.get('request_output_tokens', 0)}",
        ),
        ("original bytes", _format_bytes(stats["original_bytes"])),
        ("visible bytes", _format_bytes(stats["visible_bytes"])),
        (
            "estimated static token saving",
            str(stats.get("estimated_saved_tokens", 0) or 0),
        ),
        (
            "estimated reused token saving",
            str(stats.get("estimated_reused_tokens", 0) or 0),
        ),
        ("saved", f"{_format_bytes(stats['saved_bytes'])} ({stats['savings_ratio']:.1%})"),
        ("retrieval bytes", _format_bytes(stats["retrieval_bytes"])),
        ("effective saved", f"{effective_saved} ({effective_ratio})"),
        ("critical retention", f"{stats['critical_retention']:.1%}"),
    ]
    paths = stats.get("execution_paths") or {}
    if paths:
        rows.append(
            (
                "execution paths",
                ", ".join(f"{name}={count}" for name, count in sorted(paths.items())),
            )
        )
    reasons = stats.get("reasons") or {}
    tokens_by_reason = stats.get("tokens_by_reason") or {}
    if reasons:
        rows.append(
            (
                "decision reasons",
                ", ".join(
                    f"{name}={count}/~{int(tokens_by_reason.get(name, 0) or 0)}tok"
                    for name, count in sorted(
                        reasons.items(),
                        key=lambda item: int(tokens_by_reason.get(item[0], 0) or 0),
                        reverse=True,
                    )
                ),
            )
        )
    md = ["## Compression Diagnostics", "", "| Metric | Value |", "|---|---|"]
    md.extend(f"| {_md_escape(key)} | {_md_escape(value)} |" for key, value in rows)
    return SlashResult(
        handled=True,
        lines=[f"{key}: {value}" for key, value in rows],
        markdown="\n".join(md),
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


def _md_escape(text: str) -> str:
    """Escape pipe and backtick for Markdown table cells."""
    return str(text).replace("|", "\\|").replace("`", "\\`")


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
        lines.append(f"| {_md_escape(k)} | {_md_escape(v)} |")
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
            f"| `{_md_escape(tid)}` | {_md_escape(title)} "
            f"| {_md_escape(model)} | {_md_escape(updated)} |"
        )
    return "\n".join(lines)


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
                        "tip: /sessions — list titles; match must be unique",
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
                f"- **title**: {_md_escape(info.title)}"
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
            markdown=f"## Renamed\n\n- **title**: {_md_escape(new_title)}",
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
            lines.append(f"  … and {len(deleted) - 20} more")
        md = f"## Pruned\n\n**{len(deleted)}** empty session(s) removed.\n"
        if deleted:
            md += "\n" + "\n".join(f"- `{tid}`" for tid in deleted[:20])
            if len(deleted) > 20:
                md += f"\n- *… and {len(deleted) - 20} more*"
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
        return _handle_session("/new", [], settings=settings, agent=agent, thread_id=thread_id)

    if sub == "switch":
        return _handle_session("/switch", rest, settings=settings, agent=agent, thread_id=thread_id)

    if sub == "rename":
        return _handle_session("/rename", rest, settings=settings, agent=agent, thread_id=thread_id)

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
                    f"## Deleted\n\n- **thread_id**: `{tid}`\n- **title**: {_md_escape(label)}"
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
        return _handle_session("/export", rest, settings=settings, agent=agent, thread_id=thread_id)

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
            f"| {_md_escape(s.name)} | {_md_escape(s.transport)} "
            f"| {'yes' if s.enabled else 'no'} | {_md_escape(dest)} |"
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
            lines.append(f"- `{_md_escape(n)}`")
    for w in warnings or []:
        lines.append(f"\nwarn: {w}")
    return "\n".join(lines)


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
    )


def _mcp_attach_pending(settings: Any) -> bool:
    return bool(getattr(settings, "enable_mcp", True) and get_active_mcp_pool() is None)


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
    try:
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
        if copied:
            try:
                from synapse.models_registry import model_cache_key

                cache = getattr(agent, "_coding_model_cache", None)
                if isinstance(cache, dict):
                    stale = [key for key, value in cache.items() if value is live]
                    for key in stale:
                        cache.pop(key, None)
                    cache[model_cache_key(settings, model_name=model_name)] = live
            except Exception:  # noqa: BLE001
                pass
        return copied
    finally:
        try:
            from synapse.http_clients import close_model_async_http_client

            close_model_async_http_client(fresh)
        except Exception:  # noqa: BLE001
            pass


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
            md_lines.append(f"| {_md_escape(transport)} | {_md_escape(', '.join(names))} |")
        if result.tools:
            md_lines.append("")
            md_lines.append(f"### Tools ({len(result.tools)})")
            for tool in result.tools[:30]:
                md_lines.append(f"- `{_md_escape(getattr(tool, 'name', str(tool)))}`")
            if len(result.tools) > 30:
                md_lines.append(f"- *… and {len(result.tools) - 30} more*")
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
            f"mcp '{target}' {'enabled' if changed.enabled else 'disabled'} · tools={len(tools)}"
        )
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
    active = getattr(agent, "_coding_model_profile", None) or settings.active_model or reg.default
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
            label = apply_thinking_to_settings(settings, args[1], allowed=allowed)
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
                    defer_mcp_reconnect=True,
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
            mcp_attach_pending=bool(new_agent is not None and _mcp_attach_pending(settings)),
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
            defer_mcp_reconnect=True,
        )
    except Exception as exc:  # noqa: BLE001
        return SlashResult(
            handled=True,
            lines=[f"model switch failed: {exc}"],
            error=True,
        )
    _persist_model_binding(settings, thread_id)
    mcp_attach_pending = _mcp_attach_pending(settings)
    return SlashResult(
        handled=True,
        lines=[f"model switched to {profile.name}  ({format_model_status(settings)})"],
        agent=new_agent,
        settings_changed=True,
        mcp_attach_pending=mcp_attach_pending,
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
                f"| {mark} | `{_md_escape(t.name)}` "
                f"| {_md_escape(t.label)} | {_md_escape(kind)} |\n"
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
            f"- **name**: `{_md_escape(theme.name)}`\n"
            f"- **label**: {_md_escape(theme.label)}\n\n"
            "*saved to `~/.coding-agent/settings.json`*"
        ),
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
        return SlashResult(
            handled=True,
            lines=HELP_TEXT.splitlines(),
            markdown=HELP_TEXT,
        )

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
        result = _handle_session(cmd, args, settings=settings, agent=agent, thread_id=thread_id)
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
        md = "## Compact\n\n" + "\n".join(f"- {x}" for x in lines)
        return SlashResult(handled=True, lines=lines, error=not ok, markdown=md)

    if cmd in {"/compression", "/tool-output", "/tool-compress"}:
        return _tool_output_result(settings, thread_id, args)

    if cmd == "/context":
        from synapse.context_compact import context_status_lines

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
            md += f"| {_md_escape(k)} | {_md_escape(v)} |\n"
        return SlashResult(handled=True, lines=plain, markdown=md)

    if cmd == "/safety":
        from synapse.safety import (
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
                    md += f"| {_md_escape(k.strip())} | {_md_escape(v.strip())} |\n"
                elif line.startswith("profiles:") or line.startswith("switch:"):
                    md += f"\n*{_md_escape(line)}*\n"
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
        from synapse.skills_catalog import (
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
                md += f"| {_md_escape(s.name)} | {_md_escape(desc)} | `{_md_escape(s.path)}` |\n"
        return SlashResult(handled=True, lines=plain, markdown=md)

    if cmd == "/memory":
        from synapse.skills_catalog import (
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
                md += f"| `{_md_escape(path)}` | {size} | {status} |\n"
            md += "\n*Existing files are injected via `create_deep_agent(memory=...)`*"
        return SlashResult(handled=True, lines=plain, markdown=md)

    if cmd in {"/subagents", "/subagent"}:
        from synapse.subagents import build_default_subagents, format_subagents_lines

        specs = getattr(agent, "_coding_subagents", None)
        if specs is None:
            specs = build_default_subagents(
                enabled=getattr(settings, "enable_subagents", True),
                isolate_tools=True,
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
                    f"| {_md_escape(name)} | {_md_escape(model)} "
                    f"| {_md_escape(isolation)} "
                    f"| {_md_escape(tool_list)} |\n"
                )
        return SlashResult(handled=True, lines=plain, markdown=md)

    if raw.startswith("/"):
        return SlashResult(
            handled=True,
            lines=[f"unknown command: {cmd}", "type /help for commands"],
            error=True,
        )
    return SlashResult(handled=False)