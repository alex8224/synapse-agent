"""Compression diagnostics slash-command handler."""
from __future__ import annotations

import csv
import io
import json
import time
from pathlib import Path
from typing import Any

from synapse.commands.helpers import format_bytes, markdown_escape
from synapse.commands.result import SlashResult
from synapse.sessions.store import SessionStore


def _store(settings: Any) -> SessionStore:
    return SessionStore(settings.resolved_sessions_path())

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

    from synapse.tool_output.repository import ToolOutputRepository

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


def handle_compression(settings: Any, current_thread_id: str, args: list[str]) -> SlashResult:
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

    from synapse.tool_output.repository import ToolOutputRepository

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
                f"Turns: {stats.get('turns', 0)} — model calls: "
                f"{stats.get('model_requests', 0)} — tool calls: "
                f"{stats.get('tool_calls', 0)} — compression-managed: "
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
                f"| {markdown_escape(str(zone))} | ~{int(tokens or 0)} |"
                for zone, tokens in sorted((stats.get("live_zone_tokens") or {}).items())
            ],
            "",
            "### Tool schema ranking",
            "",
            "| Tool | Cumulative estimated tokens |",
            "|---|---:|",
            *[
                f"| {markdown_escape(str(name))} | ~{int(tokens or 0)} |"
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
                md.append(f"| {markdown_escape(str(source))} | ~{amount} | — |")
                lines.append(f"{source}=~{amount} (reference; excluded from total)")
                continue
            share = amount / total if total else 0.0
            md.append(f"| {markdown_escape(str(source))} | ~{amount} | {share:.1%} |")
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
                md.append(f"| {rank} | {markdown_escape(str(reason))} | ~{int(tokens or 0)} |")
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
                md.append(f"| {markdown_escape(str(reason))} | ~{int(tokens or 0)} |")
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
                    time=markdown_escape(str(item.get("created_at") or "-")),
                    request=markdown_escape(request_id),
                    provider=markdown_escape(str(item.get("provider") or "unknown")),
                    api=markdown_escape(str(item.get("api_style") or "unknown")),
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
                            f"| {markdown_escape(str(key))} | ~{int(value or 0)} |"
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
                            f"| {markdown_escape(str(key))} | ~{int(value or 0)} |"
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
                    time=markdown_escape(str(event.get("created_at", "-"))),
                    tool=markdown_escape(tool),
                    call_id=markdown_escape(call_id),
                    type=markdown_escape(str(event.get("content_type", "-"))),
                    decision=markdown_escape(decision),
                    reason=markdown_escape(reason),
                    transformer=markdown_escape(str(event.get("transformer", "-"))),
                    original=format_bytes(event.get("original_bytes", 0)),
                    visible=format_bytes(event.get("visible_bytes", 0)),
                    saved=f"~{saved}" if saved else "0",
                )
            )
            lines.append(
                f"{event.get('created_at', '-')} {tool}/{call_id} "
                f"{decision}:{reason} saved_tokens=~{saved}"
            )
        return SlashResult(handled=True, lines=lines, markdown="\n".join(md))

    stats = repo.stats(thread_id=thread_id)
    effective_saved = format_bytes(stats["effective_saved_bytes"])
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
        ("original bytes", format_bytes(stats["original_bytes"])),
        ("visible bytes", format_bytes(stats["visible_bytes"])),
        (
            "estimated static token saving",
            str(stats.get("estimated_saved_tokens", 0) or 0),
        ),
        (
            "estimated reused token saving",
            str(stats.get("estimated_reused_tokens", 0) or 0),
        ),
        ("saved", f"{format_bytes(stats['saved_bytes'])} ({stats['savings_ratio']:.1%})"),
        ("retrieval bytes", format_bytes(stats["retrieval_bytes"])),
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
    md.extend(f"| {markdown_escape(key)} | {markdown_escape(value)} |" for key, value in rows)
    return SlashResult(
        handled=True,
        lines=[f"{key}: {value}" for key, value in rows],
        markdown="\n".join(md),
    )
