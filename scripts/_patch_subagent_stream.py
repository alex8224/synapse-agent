"""Patch stream.py tool-message handling for subagent isolation."""
from pathlib import Path

p = Path("src/coding_agent/ui/stream.py")
text = p.read_text(encoding="utf-8")

old = '''                    if _is_tool_message(msg):
                        name = getattr(msg, "name", "tool")
                        raw_content = getattr(msg, "content", "")
                        status = summarize_tool_result(raw_content, limit=100)
                        sink.finalize_line()
                        sink.activity_stop()
                        if use_tool_items:
                            item = match_tool_result(pending_tool_items, str(name))
                            preview = truncate_preview(raw_content)
                            err = is_error_status(status, content_to_text(raw_content))
                            if item is not None:
                                item.status = "error" if err else "ok"
                                item.error = err
                                item.preview = preview
                                sink.tool_item_finished(
                                    item.id,
                                    status=status,
                                    preview=preview,
                                    error=err,
                                )
                                try:
                                    pending_tool_items.remove(item)
                                except ValueError:
                                    pass
                            else:
                                sink.tool_result(str(name), status, sub=in_sub)
                            if not pending_tool_items:
                                sink.tool_group_closed(f"g{tool_group_seq}")
                        else:
                            sink.tool_result(name, status, sub=in_sub)
                        if name in active_tools:
                            try:
                                active_tools.remove(name)
                            except ValueError:
                                pass
                        sink.activity_start(
                            "model",
                            "waiting for model"
                            if not in_sub
                            else "subagent continuing",
                        )
                        continue
'''

new = '''                    if _is_tool_message(msg):
                        name = getattr(msg, "name", "tool")
                        raw_content = getattr(msg, "content", "")
                        status = summarize_tool_result(raw_content, limit=100)
                        sink.finalize_line()
                        sink.activity_stop()
                        # Nested subgraph tool traffic must not paint the parent
                        # timeline.  Only the parent ``task`` tool result closes
                        # the "Launched subagent" group.
                        if in_sub:
                            sink.activity_update(
                                "subagent",
                                f"{name}: {status}" if status else str(name),
                            )
                            continue
                        if use_tool_items:
                            item = match_tool_result(pending_tool_items, str(name))
                            preview = truncate_preview(raw_content)
                            err = is_error_status(status, content_to_text(raw_content))
                            if item is not None:
                                item.status = "error" if err else "ok"
                                item.error = err
                                item.preview = preview
                                sink.tool_item_finished(
                                    item.id,
                                    status=status,
                                    preview=preview,
                                    error=err,
                                )
                                try:
                                    pending_tool_items.remove(item)
                                except ValueError:
                                    pass
                            # Unmatched parent results are ignored under the item
                            # API — never invent empty "0 tools" groups.
                            if not pending_tool_items:
                                sink.tool_group_closed(f"g{tool_group_seq}")
                                # Multi-round agent loop: after a tool batch the
                                # model may think / speak again.
                                sink.streamed_reasoning = False
                        else:
                            sink.tool_result(name, status, sub=False)
                        if name in active_tools:
                            try:
                                active_tools.remove(name)
                            except ValueError:
                                pass
                        sink.activity_start("model", "waiting for model")
                        continue
'''

if old not in text:
    # try with streamed_reasoning already present variant
    if "Nested subgraph tool traffic" in text:
        print("already patched")
    else:
        raise SystemExit("old tool message block not found")
else:
    text = text.replace(old, new, 1)

# Also guard tool_calls_started for nested - should not open parent groups
# Check AI message tool calls path for in_sub
marker = "if calls:"
# find AI message handling with tool calls
idx = text.find("if in_sub:\n                        if calls:")
if idx < 0:
    # try alternative
    pass

# Ensure nested AI tool calls don't open parent tool groups
old_ai = '''                    if in_sub:
                        if calls:
                            sink.activity_update(
                                "subagent",
                                f"nested tools: {', '.join(_tool_call_name(c) for c in calls[:3])}",
'''
if old_ai not in text:
    print("warn: nested AI block not exact, searching...")
else:
    print("nested AI block present")

if not text.endswith("\n"):
    text += "\n"
compile(text, str(p), "exec")
p.write_text(text, encoding="utf-8", newline="\n")
print("stream patched", len(text.splitlines()))
