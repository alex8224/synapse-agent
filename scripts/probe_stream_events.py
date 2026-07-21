"""Probe one short agent turn and print raw stream event shapes."""
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
tid = default_thread_id()
config = {"configurable": {"thread_id": tid}, "max_concurrency": 4}
payload = {
    "messages": [
        {
            "role": "user",
            "content": "只用一句话说明你是什么模型，不要调用任何工具。",
        }
    ]
}

n = 0
modes = ["messages", "updates"]
print("=== stream start ===")
try:
    for item in agent.stream(payload, config=config, stream_mode=modes, version="v2"):
        n += 1
        if n > 50:
            print("...truncated...")
            break
        if isinstance(item, tuple) and len(item) == 2:
            mode, data = item
        elif isinstance(item, dict) and "type" in item:
            mode, data = item.get("type"), item.get("data")
        else:
            mode, data = type(item).__name__, item
        print(f"\n#{n} mode={mode!r} type={type(data).__name__}")
        if mode == "messages":
            msg = data[0] if isinstance(data, tuple) else data
            meta = data[1] if isinstance(data, tuple) and len(data) > 1 else {}
            print("  cls", type(msg).__name__)
            print("  content", repr(getattr(msg, "content", None))[:240])
            print("  additional_kwargs", repr(getattr(msg, "additional_kwargs", None))[:400])
            rm = getattr(msg, "response_metadata", None) or {}
            print("  response_metadata keys", list(rm.keys())[:30])
            if rm:
                print("  response_metadata sample", repr(rm)[:400])
            print("  tool_call_chunks", getattr(msg, "tool_call_chunks", None))
            for k in ("reasoning_content", "reasoning", "type"):
                if hasattr(msg, k):
                    print(f"  attr.{k}=", repr(getattr(msg, k))[:200])
            if isinstance(meta, dict):
                print("  meta.langgraph_node", meta.get("langgraph_node"))
                print("  meta keys", list(meta.keys())[:20])
        elif mode == "updates" and isinstance(data, dict):
            for node, upd in data.items():
                msgs = (upd or {}).get("messages") if isinstance(upd, dict) else None
                print(f"  node={node} msgs={0 if not msgs else len(msgs)}")
                if msgs:
                    m = msgs[-1]
                    print("   last", type(m).__name__, "type=", getattr(m, "type", None))
                    print("   content", repr(getattr(m, "content", None))[:240])
                    print("   ak", repr(getattr(m, "additional_kwargs", None))[:400])
                    print("   tool_calls", getattr(m, "tool_calls", None))
except Exception as e:  # noqa: BLE001
    print("ERR", type(e), e)
print("=== done events", n, "===")
