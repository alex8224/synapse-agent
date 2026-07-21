"""Context compaction helpers (deepagents SummarizationToolMiddleware).

create_deep_agent already installs automatic SummarizationMiddleware.
This module adds the manual ``compact_conversation`` tool layer and product
helpers for /compact + observability.
"""

from __future__ import annotations

from typing import Any


def build_compact_tool_middleware(model: Any, backend: Any) -> Any:
    """Return SummarizationToolMiddleware (manual compact_conversation tool)."""
    from deepagents.middleware.summarization import create_summarization_tool_middleware

    return create_summarization_tool_middleware(model, backend)


def extract_summarization_event(state: Any) -> dict[str, Any] | None:
    """Best-effort pull of ``_summarization_event`` from graph state/update."""
    if state is None:
        return None
    if isinstance(state, dict):
        event = state.get("_summarization_event")
        if isinstance(event, dict):
            return event
        values = state.get("values")
        if isinstance(values, dict):
            event = values.get("_summarization_event")
            if isinstance(event, dict):
                return event
        return None
    values = getattr(state, "values", None)
    if isinstance(values, dict):
        event = values.get("_summarization_event")
        if isinstance(event, dict):
            return event
    return None


def format_summarization_event(event: dict[str, Any] | None) -> str | None:
    """One-line UI notice for a compaction event."""
    if not event:
        return None
    # Event shape varies; keep display defensive.
    cutoff = event.get("cutoff") or event.get("cutoff_index")
    path = event.get("file_path") or event.get("path") or event.get("history_path")
    summary = event.get("summary")
    bits: list[str] = ["context compacted"]
    if cutoff is not None:
        bits.append(f"cutoff={cutoff}")
    if path:
        bits.append(f"offload={path}")
    if isinstance(summary, str) and summary.strip():
        one = " ".join(summary.strip().split())
        if len(one) > 80:
            one = one[:79] + "…"
        bits.append(f"summary={one}")
    return " | ".join(bits)


def force_compact_via_agent(
    agent: Any,
    *,
    thread_id: str,
    config: dict[str, Any] | None = None,
) -> tuple[bool, list[str]]:
    """Ask the agent to call ``compact_conversation`` now.

    Returns (ok, status_lines). The tool may refuse when under its eligibility
    gate (~50% of auto-summarization trigger).
    """
    if agent is None or not thread_id:
        return False, ["compact failed: missing agent/thread_id"]

    run_config = dict(config or {})
    cfg = dict(run_config.get("configurable") or {})
    cfg["thread_id"] = thread_id
    run_config["configurable"] = cfg

    prompt = (
        "System instruction for this turn only: call the `compact_conversation` "
        "tool immediately to compact the conversation context. Do not call other "
        "tools. After the tool returns, reply with one short status line only "
        "(success, refused/not eligible, or error)."
    )
    payload = {"messages": [{"role": "user", "content": prompt}]}
    try:
        result = agent.invoke(payload, run_config)
    except Exception as exc:  # noqa: BLE001
        return False, [f"compact failed: {exc}"]

    lines: list[str] = []
    event = extract_summarization_event(result)
    note = format_summarization_event(event)
    if note:
        lines.append(note)

    # Pull last AI / tool text for user feedback.
    messages = []
    if isinstance(result, dict):
        messages = list(result.get("messages") or [])
    text_bits: list[str] = []
    for msg in reversed(messages[-8:]):
        role = getattr(msg, "type", None) or getattr(msg, "role", None) or ""
        name = getattr(msg, "name", None) or ""
        content = getattr(msg, "content", None)
        if content is None:
            continue
        body = content if isinstance(content, str) else str(content)
        body = body.strip()
        if not body:
            continue
        if str(role).lower() in {"tool", "toolmessage"} or name == "compact_conversation":
            text_bits.append(body[:300])
            break
        if str(role).lower() in {"ai", "assistant"}:
            text_bits.append(body[:300])
            break
    if text_bits:
        lines.append(text_bits[0])
    if not lines:
        lines.append("compact requested (no status detail)")
    ok = any(
        "compact" in (ln or "").casefold()
        or "summar" in (ln or "").casefold()
        or "nothing to compact" in (ln or "").casefold()
        or "not eligible" in (ln or "").casefold()
        or "success" in (ln or "").casefold()
        for ln in lines
    )
    return True if lines else ok, lines


def context_status_lines(agent: Any, thread_id: str) -> list[str]:
    """Report message count + last summarization event for current thread."""
    lines: list[str] = [f"thread_id={thread_id}"]
    if agent is None:
        return lines + ["agent: none"]
    get_state = getattr(agent, "get_state", None)
    if not callable(get_state):
        return lines + ["state: unavailable"]
    try:
        state = get_state({"configurable": {"thread_id": thread_id}})
    except Exception as exc:  # noqa: BLE001
        return lines + [f"state error: {exc}"]
    values = getattr(state, "values", None) or {}
    messages = values.get("messages") if isinstance(values, dict) else None
    n = len(messages or [])
    lines.append(f"messages={n}")
    event = extract_summarization_event(state)
    note = format_summarization_event(event)
    if note:
        lines.append(note)
    else:
        lines.append("summarization: none yet (auto middleware still active)")
    return lines


# --- UI filters: hide SESSION INTENT / SUMMARY compaction text from timeline ---

_SUMMARY_MARKERS = (
    "## SESSION INTENT",
    "SESSION INTENT",
    "## SUMMARY",
)
_WRAPPER_PREFIXES = (
    "Here is a summary of the conversation to date:",
    "You are in the middle of a conversation that has been summarized",
)


def is_lc_summarization_message(msg: Any) -> bool:
    """True when message was tagged as summarization middleware output."""
    ak = getattr(msg, "additional_kwargs", None)
    if isinstance(ak, dict) and ak.get("lc_source") == "summarization":
        return True
    if isinstance(msg, dict):
        ak2 = msg.get("additional_kwargs") or {}
        if isinstance(ak2, dict) and ak2.get("lc_source") == "summarization":
            return True
        md = msg.get("response_metadata") or msg.get("metadata") or {}
        if isinstance(md, dict) and md.get("lc_source") == "summarization":
            return True
    md2 = getattr(msg, "response_metadata", None)
    if isinstance(md2, dict) and md2.get("lc_source") == "summarization":
        return True
    return False


def is_stream_meta_summarization(meta: Any) -> bool:
    """True when LangGraph messages-stream meta marks a summarization invoke."""
    if not isinstance(meta, dict):
        return False
    for key in ("lc_source", "source"):
        if meta.get(key) == "summarization":
            return True
    nested = meta.get("metadata") or meta.get("ls_metadata") or {}
    if isinstance(nested, dict) and nested.get("lc_source") == "summarization":
        return True
    tags = meta.get("tags") or (
        nested.get("tags") if isinstance(nested, dict) else None
    )
    if isinstance(tags, (list, tuple, set)):
        if "summarization" in tags or "lc:summarization" in tags:
            return True
    return False


def is_context_compact_text(text: str | None) -> bool:
    """Heuristic: body looks like a context-compaction summary, not a user reply."""
    body = (text or "").strip()
    if not body:
        return False
    head = body[:800]
    for p in _WRAPPER_PREFIXES:
        if body.startswith(p) or p in head:
            if any(
                m in body
                for m in ("SESSION INTENT", "SUMMARY", "ARTIFACTS", "NEXT STEPS")
            ):
                return True
            if body.startswith(p):
                return True

    # DEFAULT_SUMMARY_PROMPT sections (with or without markdown ##).
    has_intent = (
        "## SESSION INTENT" in body
        or body.lstrip("# ").startswith("SESSION INTENT")
        or "\nSESSION INTENT\n" in f"\n{body}\n"
    )
    has_summary = (
        "## SUMMARY" in body
        or "\n## SUMMARY" in body
        or "\nSUMMARY\n" in f"\n{body}\n"
        or "\n# SUMMARY\n" in f"\n{body}\n"
    )
    if has_intent and has_summary:
        return True
    if has_intent and ("ARTIFACTS" in body or "NEXT STEPS" in body):
        return True
    return False

