"""Count what stream_agent event iterator actually yields."""
from __future__ import annotations

from pathlib import Path

from coding_agent.agent import build_coding_agent, default_thread_id
from coding_agent.config import bootstrap_project_env, load_settings
from coding_agent.ui.stream import (
    _extract_reasoning,
    _is_ai_message,
    _is_tool_message,
    _iter_stream_events,
    _normalize_content,
)

bootstrap_project_env(Path.cwd())
settings = load_settings(
    workspace=str(Path.cwd()), checkpoint_backend="memory", require_approval=False
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

counts = {"messages": 0, "updates": 0, "heartbeat": 0, "other": 0}
content_msgs = 0
ai_with_text = 0
ai_with_tools = 0
tool_msgs = 0

for mode, chunk, ns in _iter_stream_events(
    agent,
    payload,
    config,
    token_stream=True,
    prefer_async=True,
    subgraphs=True,
):
    if mode == "__heartbeat__":
        counts["heartbeat"] += 1
        continue
    if mode == "messages":
        counts["messages"] += 1
        msg = chunk[0] if isinstance(chunk, tuple) else chunk
        text = _normalize_content(getattr(msg, "content", ""))
        reason = _extract_reasoning(msg)
        tcc = getattr(msg, "tool_call_chunks", None) or []
        if text:
            content_msgs += 1
            if content_msgs <= 5:
                print("TOKEN_TEXT", repr(text)[:100])
        if reason and content_msgs <= 3:
            print("TOKEN_REASON", repr(reason)[:80])
        if tcc and content_msgs <= 3:
            print("TOKEN_TCC", tcc[:1])
    elif mode == "updates":
        counts["updates"] += 1
        if not isinstance(chunk, dict):
            print("UPD non-dict", type(chunk))
            continue
        # print node keys once
        for node, upd in (
            chunk.items()
            if all(isinstance(v, dict) for v in chunk.values())
            else [("?", chunk)]
        ):
            if not isinstance(upd, dict):
                continue
            for m in upd.get("messages") or []:
                if _is_tool_message(m):
                    tool_msgs += 1
                    print("TOOL", getattr(m, "name", None))
                elif _is_ai_message(m):
                    text = _normalize_content(getattr(m, "content", "")).strip()
                    calls = getattr(m, "tool_calls", None) or []
                    if text:
                        ai_with_text += 1
                        print("AI_TEXT", repr(text)[:160], "tools", len(calls))
                    if calls:
                        ai_with_tools += 1
                        print("AI_TOOLS", [c.get("name") if isinstance(c, dict) else c for c in calls])
    else:
        counts["other"] += 1
        print("OTHER", mode, type(chunk))

print("COUNTS", counts)
print(
    "content_token_chunks",
    content_msgs,
    "ai_with_text",
    ai_with_text,
    "ai_with_tools",
    ai_with_tools,
    "tool_msgs",
    tool_msgs,
)
