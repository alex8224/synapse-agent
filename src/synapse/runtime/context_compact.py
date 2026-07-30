"""Context compaction helpers for deepagents automatic summarization.

``create_deep_agent`` installs ``SummarizationMiddleware`` for threshold-based
compaction. This module exposes the ``/compact`` trigger and observability
helpers without registering a model-callable compaction tool.
"""

from __future__ import annotations

from typing import Any


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
    """Force the built-in ``SummarizationMiddleware`` for ``/compact`` only."""
    if agent is None or not thread_id:
        return False, ["compact failed: missing agent/thread_id"]

    run_config = dict(config or {})
    cfg = dict(run_config.get("configurable") or {})
    cfg["thread_id"] = thread_id
    run_config["configurable"] = cfg

    middleware = _find_summarization_middleware(agent)
    if middleware is None:
        return False, ["compact failed: automatic summarization middleware unavailable"]

    original_should_summarize = getattr(middleware, "_should_summarize", None)
    if not callable(original_should_summarize):
        return False, ["compact failed: automatic summarization middleware cannot be forced"]

    # The built-in middleware compacts just before a model call. Override its
    # threshold for this one slash-command invocation, then restore it even if
    # the invoke fails. No model-callable compact tool is registered.
    middleware._should_summarize = lambda _messages, _total_tokens: True
    try:
        payload = {"messages": [{"role": "user", "content": "Compact the current context."}]}
        ainvoke = getattr(agent, "ainvoke", None)
        runtime = getattr(agent, "_coding_async_runtime", None)
        if callable(ainvoke) and runtime is not None:
            result = runtime.run(ainvoke(payload, run_config))
        else:
            result = agent.invoke(payload, run_config)
    except Exception as exc:  # noqa: BLE001
        return False, [f"compact failed: {exc}"]
    finally:
        middleware._should_summarize = original_should_summarize

    note = format_summarization_event(extract_summarization_event(result))
    return True, [note or "context compacted"]


def _find_summarization_middleware(agent: Any) -> Any | None:
    """Return deepagents' built-in automatic summarization middleware."""
    nodes = getattr(agent, "nodes", None)
    node = nodes.get("model") if isinstance(nodes, dict) else None
    middleware = getattr(node, "bound", None)
    for candidate in getattr(middleware, "middleware", ()):
        if getattr(candidate, "name", None) == "SummarizationMiddleware":
            return candidate
    return None


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

