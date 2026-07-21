"""Keep multi-turn context continuous after user cancel (ESC).

Hard-stopping astream can leave the LangGraph checkpoint with:
- AIMessage.tool_calls without matching ToolMessage
- pending ``next`` nodes (e.g. tools / model)

The next user turn then fails or loses consistency. This module seals the
cancelled turn so prior messages remain usable.
"""

from __future__ import annotations

from typing import Any


def _msg_type(msg: Any) -> str:
    t = getattr(msg, "type", None)
    if t:
        return str(t).lower()
    return type(msg).__name__.lower()


def _is_tool_msg(msg: Any) -> bool:
    return _msg_type(msg) in {"tool", "toolmessage"}


def _is_ai_msg(msg: Any) -> bool:
    return _msg_type(msg) in {"ai", "assistant", "aimessage"}


def _is_human_msg(msg: Any) -> bool:
    return _msg_type(msg) in {"human", "user", "humanmessage"}


def _tool_call_id(call: Any) -> str | None:
    if isinstance(call, dict):
        cid = call.get("id")
    else:
        cid = getattr(call, "id", None)
    return str(cid) if cid else None


def _tool_call_name(call: Any) -> str:
    if isinstance(call, dict):
        name = call.get("name")
    else:
        name = getattr(call, "name", None)
    return str(name or "tool")


def _graph_nodes(agent: Any) -> set[str]:
    nodes = getattr(agent, "nodes", None) or {}
    try:
        return set(nodes.keys())
    except Exception:  # noqa: BLE001
        return set()


def _pick_tools_node(nodes: set[str], next_nodes: tuple[str, ...]) -> str | None:
    for n in next_nodes:
        if "tool" in n.lower():
            return n
    if "tools" in nodes:
        return "tools"
    for n in nodes:
        if "tool" in n.lower() and not n.startswith("__"):
            return n
    return None


def _pick_model_node(nodes: set[str], next_nodes: tuple[str, ...]) -> str | None:
    for n in next_nodes:
        low = n.lower()
        if "tool" not in low and not n.startswith("__"):
            return n
    for preferred in ("model", "agent"):
        if preferred in nodes:
            return preferred
    for n in nodes:
        if not n.startswith("__") and "tool" not in n.lower() and "middleware" not in n.lower():
            return n
    return None


def _answered_tool_ids(messages: list[Any]) -> set[str]:
    done: set[str] = set()
    for msg in messages:
        if not _is_tool_msg(msg):
            continue
        tid = getattr(msg, "tool_call_id", None)
        if tid:
            done.add(str(tid))
    return done


def _pending_tool_seals(messages: list[Any]) -> list[Any]:
    from langchain_core.messages import ToolMessage

    answered = _answered_tool_ids(messages)
    seals: list[Any] = []
    for msg in messages:
        for call in getattr(msg, "tool_calls", None) or []:
            cid = _tool_call_id(call)
            if not cid or cid in answered:
                continue
            seals.append(
                ToolMessage(
                    content="[cancelled by user]",
                    tool_call_id=cid,
                    name=_tool_call_name(call),
                    status="error",
                )
            )
            answered.add(cid)
    return seals


def _needs_cancel_note(messages: list[Any]) -> bool:
    if not messages:
        return False
    last = messages[-1]
    content = str(getattr(last, "content", "") or "")
    low = content.casefold()
    if _is_ai_msg(last) and not (getattr(last, "tool_calls", None) or []):
        if "cancelled" in low or "终止" in content:
            return False
        # Plain AI reply already closed the turn.
        if content.strip():
            return False
        return True
    # Human without answer, tool without follow-up AI, or AI with open tools
    # (tools should already be sealed before this check).
    return True


def repair_thread_after_cancel(agent: Any, config: dict[str, Any]) -> list[str]:
    """Seal a cancelled turn so the same thread_id remains continuous.

    Returns short status notes for logs/UI (never raises).
    """
    notes: list[str] = []
    get_state = getattr(agent, "get_state", None)
    update_state = getattr(agent, "update_state", None)
    if not callable(get_state) or not callable(update_state):
        return ["cancel seal skipped: agent has no get_state/update_state"]

    try:
        snap = get_state(config)
    except Exception as exc:  # noqa: BLE001
        return [f"cancel seal skipped: get_state failed: {exc}"]

    values = getattr(snap, "values", None) or {}
    messages = list(values.get("messages") or [])
    if not messages:
        return ["cancel seal: empty thread (nothing to preserve)"]

    nodes = _graph_nodes(agent)
    next_nodes = tuple(str(x) for x in (getattr(snap, "next", None) or ()))

    try:
        seals = _pending_tool_seals(messages)
        if seals:
            tools_node = _pick_tools_node(nodes, next_nodes)
            if tools_node:
                update_state(config, {"messages": seals}, as_node=tools_node)
                notes.append(f"sealed {len(seals)} open tool call(s) as_node={tools_node}")
            else:
                update_state(config, {"messages": seals})
                notes.append(f"sealed {len(seals)} open tool call(s)")
            snap = get_state(config)
            values = getattr(snap, "values", None) or {}
            messages = list(values.get("messages") or [])
            next_nodes = tuple(str(x) for x in (getattr(snap, "next", None) or ()))

        if _needs_cancel_note(messages):
            from langchain_core.messages import AIMessage

            note = AIMessage(content="[本轮已由用户终止，上下文已保留]")
            model_node = _pick_model_node(nodes, next_nodes)
            if model_node and next_nodes:
                try:
                    update_state(config, {"messages": [note]}, as_node=model_node)
                    notes.append(f"cancel note as_node={model_node}")
                except Exception:  # noqa: BLE001
                    update_state(config, {"messages": [note]})
                    notes.append("cancel note appended")
            else:
                # No pending graph step: still append a boundary note when the
                # last message is human/tool so the next model call is clean.
                last = messages[-1]
                if _is_human_msg(last) or _is_tool_msg(last) or (
                    _is_ai_msg(last) and (getattr(last, "tool_calls", None) or [])
                ):
                    update_state(config, {"messages": [note]})
                    notes.append("cancel note appended")

        snap = get_state(config)
        values = getattr(snap, "values", None) or {}
        messages = list(values.get("messages") or [])
        next_nodes = tuple(str(x) for x in (getattr(snap, "next", None) or ()))
        notes.append(f"messages={len(messages)}")
        if next_nodes:
            notes.append(f"pending next={next_nodes}")
        else:
            notes.append("checkpoint ready for next turn")
    except Exception as exc:  # noqa: BLE001
        notes.append(f"cancel seal error: {exc}")
    return notes
