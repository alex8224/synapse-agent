"""Deeper probe of empty AIMessageChunks and tool-turn stream."""
from __future__ import annotations

from pathlib import Path

from coding_agent.agent import build_coding_agent, default_thread_id
from coding_agent.config import bootstrap_project_env, load_settings

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

# 1) no tools
payload = {
    "messages": [
        {"role": "user", "content": "只说：ok。不要工具。"}
    ]
}
print("=== SIMPLE ===")
empty_dumps = 0
for item in agent.stream(
    payload, config=config, stream_mode=["messages", "updates"], version="v2"
):
    if not (isinstance(item, tuple) and len(item) == 2 and item[0] == "messages"):
        if isinstance(item, tuple) and len(item) == 2 and item[0] == "updates":
            data = item[1]
            if isinstance(data, dict):
                for node, upd in data.items():
                    msgs = upd.get("messages") if isinstance(upd, dict) else None
                    if msgs:
                        m = msgs[-1]
                        print(
                            "UPD",
                            node,
                            type(m).__name__,
                            "content=",
                            repr(getattr(m, "content", None))[:120],
                            "ak=",
                            repr(getattr(m, "additional_kwargs", {}))[:200],
                            "tools=",
                            bool(getattr(m, "tool_calls", None)),
                        )
        continue
    msg, meta = item[1]
    content = getattr(msg, "content", None)
    ak = getattr(msg, "additional_kwargs", {}) or {}
    rm = getattr(msg, "response_metadata", {}) or {}
    if content in ("", None) and not ak and not getattr(msg, "tool_call_chunks", None):
        empty_dumps += 1
        if empty_dumps <= 3:
            dump = None
            if hasattr(msg, "model_dump"):
                dump = msg.model_dump()
            print("EMPTY_CHUNK dump", dump)
            print("EMPTY meta", meta)
        continue
    print(
        "TOK",
        "content=",
        repr(content)[:80],
        "ak=",
        repr(ak)[:200],
        "rm_keys=",
        list(rm.keys())[:15],
        "tcc=",
        getattr(msg, "tool_call_chunks", None),
    )

print("empty_count", empty_dumps)

# 2) force tools
print("\n=== WITH TOOLS ===")
payload2 = {
    "messages": [
        {
            "role": "user",
            "content": "只调用一次 ls 查看当前目录，然后用一句话总结，不要再多工具。",
        }
    ]
}
config2 = {
    "configurable": {"thread_id": default_thread_id()},
    "max_concurrency": 4,
}
seen = 0
for item in agent.stream(
    payload2, config=config2, stream_mode=["messages", "updates"], version="v2"
):
    seen += 1
    if seen > 80:
        print("truncated")
        break
    if isinstance(item, tuple) and len(item) == 2:
        mode, data = item
    else:
        continue
    if mode == "messages":
        msg, meta = data
        content = getattr(msg, "content", None)
        ak = getattr(msg, "additional_kwargs", {}) or {}
        tcc = getattr(msg, "tool_call_chunks", None)
        if content or ak or tcc:
            print(
                "MSG",
                "node=",
                (meta or {}).get("langgraph_node"),
                "content=",
                repr(content)[:100],
                "ak=",
                repr(ak)[:180],
                "tcc=",
                tcc,
            )
    elif mode == "updates" and isinstance(data, dict):
        for node, upd in data.items():
            msgs = upd.get("messages") if isinstance(upd, dict) else None
            if not msgs:
                continue
            for m in msgs:
                print(
                    "UPD",
                    node,
                    type(m).__name__,
                    "name=",
                    getattr(m, "name", None),
                    "content=",
                    repr(getattr(m, "content", None))[:120],
                    "ak=",
                    repr(getattr(m, "additional_kwargs", {}))[:160],
                    "tool_calls=",
                    getattr(m, "tool_calls", None),
                )
print("done", seen)
