"""Probe when AI text exists vs when our printer would show it."""
from __future__ import annotations

from pathlib import Path

from coding_agent.agent import build_coding_agent, default_thread_id
from coding_agent.config import bootstrap_project_env, load_settings
from coding_agent.ui.stream import (
    _extract_reasoning,
    _is_ai_message,
    _is_tool_message,
    _normalize_content,
    stream_agent,
)

bootstrap_project_env(Path.cwd())
settings = load_settings(
    workspace=str(Path.cwd()),
    checkpoint_backend="memory",
    require_approval=False,
)
agent = build_coding_agent(settings, project_root=Path.cwd())
config = {
    "configurable": {"thread_id": default_thread_id()},
    "max_concurrency": 4,
}
payload = {
    "messages": [
        {
            "role": "user",
            "content": "先用一句话说明你要做什么，然后调用一次 ls 查看 / ，最后再给一句结论。",
        }
    ]
}

print("=== RAW EVENTS (messages/updates) ===")
ai_events = 0
for item in agent.stream(
    payload, config=config, stream_mode=["messages", "updates"], version="v2"
):
    if not (isinstance(item, tuple) and len(item) == 2):
        continue
    mode, data = item
    if mode == "messages":
        msg, meta = data
        content = _normalize_content(getattr(msg, "content", ""))
        reasoning = _extract_reasoning(msg)
        tcc = getattr(msg, "tool_call_chunks", None) or []
        node = (meta or {}).get("langgraph_node")
        if content or reasoning or tcc:
            print(
                f"MSG node={node!r} content={content!r:.120} "
                f"reason_len={len(reasoning)} tcc={bool(tcc)}"
            )
            ai_events += 1
    elif mode == "updates" and isinstance(data, dict):
        for node, upd in data.items():
            msgs = (upd or {}).get("messages") if isinstance(upd, dict) else None
            if not msgs:
                continue
            for m in msgs:
                if _is_tool_message(m):
                    print(f"UPD {node} TOOL name={getattr(m,'name',None)} content_len={len(str(getattr(m,'content','')))}")
                elif _is_ai_message(m):
                    content = _normalize_content(getattr(m, "content", "")).strip()
                    reasoning = _extract_reasoning(m)
                    calls = getattr(m, "tool_calls", None) or []
                    print(
                        f"UPD {node} AI content={content!r:.160} "
                        f"reason_len={len(reasoning)} tool_calls={len(calls)}"
                    )
                    ai_events += 1

print("raw_ai_related_events", ai_events)

print("\n=== VIA stream_agent ===")
# fresh thread
config2 = {
    "configurable": {"thread_id": default_thread_id()},
    "max_concurrency": 4,
}
payload2 = {
    "messages": [
        {
            "role": "user",
            "content": "先用一句话说明你要做什么，然后调用一次 ls 查看 / ，最后再给一句结论。",
        }
    ]
}
result = stream_agent(
    agent,
    payload2,
    config2,
    token_stream=True,
    prefer_async=True,
    max_concurrency=4,
)
print("streamed_answer", result.streamed_answer)
print("final_text", repr(result.final_text[:200]))
print("reasoning_len", len(result.reasoning_text))
print("tool_calls", result.tool_calls)
