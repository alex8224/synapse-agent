"""Load conversation transcript from LangGraph checkpointer."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from synapse.context_compact import (
    is_context_compact_text,
    is_lc_summarization_message,
)


def _message_role(msg: Any) -> str:
    t = getattr(msg, "type", None) or getattr(msg, "role", None)
    if t:
        return str(t).lower()
    if isinstance(msg, dict):
        return str(msg.get("type") or msg.get("role") or "unknown").lower()
    cls = msg.__class__.__name__.lower()
    if "human" in cls:
        return "human"
    if "ai" in cls or "assistant" in cls:
        return "ai"
    if "system" in cls:
        return "system"
    if "tool" in cls:
        return "tool"
    return "unknown"


_NON_TEXT_BLOCK_TYPES = frozenset(
    {
        "tool_use",
        "tool_call",
        "tool_result",
        "input_json",
        "input_json_delta",
        "function_call",
        "server_tool_use",
        "mcp_tool_use",
        "mcp_tool_result",
        "image",
        "image_url",
        "file",
        "document",
        "reasoning",
        "thinking",
        "redacted_thinking",
    }
)


def _looks_like_tool_payload(text: str) -> bool:
    """Heuristic: string is a serialized tool_use / tool_calls blob."""
    one = (text or "").strip()
    if not one:
        return False
    if '"type": "tool_use"' in one or '"type":"tool_use"' in one:
        return True
    if '"partial_json"' in one and '"name"' in one:
        return True
    if one.startswith("{") and '"tool_use"' in one and '"input"' in one:
        return True
    if one.startswith("{") and '"todos"' in one and '"status"' in one and len(one) > 80:
        try:
            data = json.loads(one)
        except Exception:  # noqa: BLE001
            return False
        return isinstance(data, dict) and "todos" in data
    return False


def _message_content(msg: Any) -> str:
    """Extract human-visible text only (never dump tool_use / JSON blocks)."""
    content = getattr(msg, "content", None)
    if content is None and isinstance(msg, dict):
        content = msg.get("content")
    if content is None:
        return ""
    if isinstance(content, str):
        if _looks_like_tool_payload(content):
            return ""
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                if not _looks_like_tool_payload(block):
                    parts.append(block)
                continue
            if isinstance(block, dict):
                btype = str(block.get("type") or "").casefold()
                if btype in _NON_TEXT_BLOCK_TYPES:
                    continue
                if block.get("name") and (
                    "input" in block or "partial_json" in block or btype == "tool_use"
                ):
                    continue
                if btype in {"text", "output_text", "input_text"} or "text" in block:
                    text = block.get("text")
                    if text:
                        parts.append(str(text))
                    continue
                # Unknown dict: never json-dump (restore leak source).
                continue
            text = getattr(block, "text", None)
            btype = str(getattr(block, "type", "") or "").casefold()
            if btype in _NON_TEXT_BLOCK_TYPES:
                continue
            if text:
                parts.append(str(text))
        return "\n".join(parts)
    return str(content)


def _message_reasoning(msg: Any) -> str:
    """Best-effort reasoning/thinking extraction for replay."""
    parts: list[str] = []
    for src in (
        getattr(msg, "additional_kwargs", None),
        getattr(msg, "response_metadata", None),
    ):
        if not isinstance(src, dict):
            continue
        for key in ("reasoning_content", "reasoning", "thinking", "thought"):
            val = src.get(key)
            if val:
                parts.append(str(val))
    content = getattr(msg, "content", None)
    if content is None and isinstance(msg, dict):
        content = msg.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = str(block.get("type") or "")
            if btype in {"reasoning", "thinking"}:
                parts.append(str(block.get("text") or block.get("reasoning") or ""))
    for key in ("reasoning_content", "reasoning"):
        val = getattr(msg, key, None)
        if val:
            parts.append(str(val))
    # de-dupe preserve order
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return "".join(out)


def _parse_tool_args(raw_args: Any) -> dict[str, Any]:
    if raw_args is None:
        return {}
    if isinstance(raw_args, dict):
        return raw_args
    if isinstance(raw_args, str):
        text = raw_args.strip()
        if not text:
            return {}
        try:
            data = json.loads(text)
            return data if isinstance(data, dict) else {"value": data}
        except Exception:  # noqa: BLE001
            return {"arguments": raw_args}
    return {"value": raw_args}


def _tool_calls_from_content_blocks(content: Any) -> list[dict[str, Any]]:
    """Anthropic-style tool_use blocks live inside message.content list."""
    if not isinstance(content, list):
        return []
    out: list[dict[str, Any]] = []
    for i, block in enumerate(content):
        if isinstance(block, dict):
            btype = str(block.get("type") or "").casefold()
            name = block.get("name")
            if btype not in {"tool_use", "tool_call", "function_call", "server_tool_use"}:
                # Some serializers omit type but keep tool shape.
                if not (name and ("input" in block or "partial_json" in block)):
                    continue
            cid = str(block.get("id") or block.get("tool_use_id") or f"block-{i}")
            tname = str(name or block.get("function", {}).get("name") or "?")
            args = block.get("input")
            if args is None:
                args = block.get("args")
            if args is None and block.get("partial_json"):
                args = _parse_tool_args(block.get("partial_json"))
            else:
                args = _parse_tool_args(args)
            out.append({"id": cid, "name": tname, "args": args})
            continue
        btype = str(getattr(block, "type", "") or "").casefold()
        if btype not in {"tool_use", "tool_call", "function_call"}:
            continue
        cid = str(getattr(block, "id", None) or f"block-{i}")
        tname = str(getattr(block, "name", None) or "?")
        args = getattr(block, "input", None)
        if args is None:
            args = getattr(block, "args", None)
        out.append({"id": cid, "name": tname, "args": _parse_tool_args(args)})
    return out


def _tool_calls(msg: Any) -> list[dict[str, Any]]:
    raw = getattr(msg, "tool_calls", None)
    if raw is None and isinstance(msg, dict):
        raw = msg.get("tool_calls")
    if not raw:
        # OpenAI-style additional_kwargs
        ak = getattr(msg, "additional_kwargs", None) or {}
        if isinstance(ak, dict):
            raw = ak.get("tool_calls") or []
    out: list[dict[str, Any]] = []
    for i, call in enumerate(raw or []):
        if isinstance(call, dict):
            cid = str(call.get("id") or call.get("tool_call_id") or f"call-{i}")
            name = str(call.get("name") or call.get("function", {}).get("name") or "?")
            args = call.get("args")
            if args is None:
                args = call.get("input")
            if args is None:
                fn = call.get("function") or {}
                args = fn.get("arguments") if isinstance(fn, dict) else None
            out.append({"id": cid, "name": name, "args": _parse_tool_args(args)})
            continue
        cid = str(getattr(call, "id", None) or f"call-{i}")
        name = str(getattr(call, "name", None) or "?")
        args = getattr(call, "args", None)
        if args is None:
            args = getattr(call, "input", None)
        out.append({"id": cid, "name": name, "args": _parse_tool_args(args)})

    # Merge Anthropic content-block tool_use (avoid duplicates by id).
    content = getattr(msg, "content", None)
    if content is None and isinstance(msg, dict):
        content = msg.get("content")
    seen = {str(c.get("id") or "") for c in out}
    for call in _tool_calls_from_content_blocks(content):
        cid = str(call.get("id") or "")
        if cid and cid in seen:
            continue
        out.append(call)
        if cid:
            seen.add(cid)
    return out


def message_to_export_dict(msg: Any) -> dict[str, Any]:
    if isinstance(msg, dict):
        role = str(msg.get("type") or msg.get("role") or "unknown")
        content = msg.get("content", "")
        if not isinstance(content, str):
            content = _message_content(msg)
        return {"role": role, "content": content}
    return {
        "role": _message_role(msg),
        "content": _message_content(msg),
        "id": getattr(msg, "id", None),
        "name": getattr(msg, "name", None),
    }


def load_messages_from_checkpointer(
    checkpointer: Any,
    thread_id: str,
    *,
    max_parents: int = 50,
) -> list[Any]:
    """从 LangGraph checkpointer 加载 thread 的消息。

    先从最新 checkpoint 的 channel_values 中取 messages。
    若不存在（上下文压缩后），沿 parent_config 链向上回退，
    找到第一个包含 messages 的 checkpoint 后返回。

    Args:
        checkpointer: LangGraph checkpointer 实例
        thread_id: 会话 ID
        max_parents: 最多回退的父 checkpoint 数，防止无限循环
    """
    if checkpointer is None or not thread_id:
        return []
    get_tuple = getattr(checkpointer, "get_tuple", None)
    if not callable(get_tuple):
        return []

    config: dict[str, Any] = {"configurable": {"thread_id": thread_id}}
    for _ in range(max(1, max_parents + 1)):
        try:
            tup = get_tuple(config)
        except Exception:  # noqa: BLE001
            return []
        if tup is None:
            return []

        checkpoint = getattr(tup, "checkpoint", None) or {}
        values = checkpoint.get("channel_values") or {}
        messages = values.get("messages")
        if messages:
            return list(messages)

        # 当前 checkpoint 无 messages（被压缩），向上追溯
        parent = getattr(tup, "parent_config", None)
        if parent is None:
            return []
        config = parent

    return []


def load_messages_from_sqlite_file(
    checkpoint_path: Path | str,
    thread_id: str,
    *,
    max_parents: int = 50,
) -> list[Any]:
    """从 SqliteSaver 数据库加载 thread 的消息。

    当最新 checkpoint 的消息已被上下文压缩清空时，沿父 checkpoint 链
    向上回退，直到找到包含完整 messages 的快照。

    Args:
        checkpoint_path: checkpoints.sqlite 路径
        thread_id: 会话 ID
        max_parents: 最多回退的父 checkpoint 数
    """
    path = Path(checkpoint_path).expanduser()
    if not path.is_file():
        return []
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver
    except Exception:  # noqa: BLE001
        return []

    conn = sqlite3.connect(str(path), check_same_thread=False)
    try:
        saver = SqliteSaver(conn)
        messages = load_messages_from_checkpointer(
            saver, thread_id, max_parents=max_parents
        )
        if messages:
            return messages

        # 最终回退：解析 deepagents 压缩导出的 conversation_history Markdown
        history_md = path.parent / ".." / "conversation_history" / f"{thread_id}.md"
        try:
            resolved = history_md.resolve()
        except Exception:  # noqa: BLE001
            resolved = None
        if resolved and resolved.is_file():
            return parse_conversation_history_md(resolved)
        return []
    except Exception:  # noqa: BLE001
        return []
    finally:
        conn.close()


def load_messages_from_agent(agent: Any, thread_id: str) -> list[Any]:
    """Load messages via agent.get_state when available."""
    if agent is None or not thread_id:
        return []
    get_state = getattr(agent, "get_state", None)
    if not callable(get_state):
        # Some compiled graphs expose this on the runnable.
        return []
    try:
        state = get_state({"configurable": {"thread_id": thread_id}})
        values = getattr(state, "values", None) or {}
        if isinstance(values, dict):
            messages = values.get("messages") or []
            return list(messages)
    except Exception:  # noqa: BLE001
        return []
    return []


def load_thread_messages(
    *,
    agent: Any = None,
    settings: Any = None,
    thread_id: str,
    checkpointer: Any = None,
) -> list[Any]:
    """Load thread messages: agent state → checkpointer → sqlite file."""
    messages = load_messages_from_agent(agent, thread_id)
    if messages:
        return messages
    cp = checkpointer or getattr(agent, "_coding_checkpointer", None)
    messages = load_messages_from_checkpointer(cp, thread_id)
    if messages:
        return messages
    if settings is not None:
        path = getattr(settings, "checkpoint_path", None)
        backend = getattr(settings, "checkpoint_backend", "sqlite")
        if path is not None and backend == "sqlite":
            return load_messages_from_sqlite_file(path, thread_id)
    return []


@dataclass
class UiTranscriptEvent:
    """One renderable unit for TUI/history replay."""

    kind: str  # user | answer | thought | tools | meta
    text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    # Optional inline images for user turns: (raw_bytes, mime)
    images: list[tuple[bytes, str]] = field(default_factory=list)



def _message_images(msg: Any) -> list[tuple[bytes, str]]:
    """Best-effort image bytes from a human multimodal message."""
    content = getattr(msg, "content", None)
    if content is None and isinstance(msg, dict):
        content = msg.get("content")
    try:
        from synapse.multimodal import extract_image_payloads
    except Exception:  # noqa: BLE001
        return []
    try:
        return extract_image_payloads(content)
    except Exception:  # noqa: BLE001
        return []


def fold_messages_for_ui(messages: list[Any]) -> list[UiTranscriptEvent]:
    """Collapse LangChain messages into TUI-friendly events."""
    events: list[UiTranscriptEvent] = []
    pending_calls: list[dict[str, Any]] = []
    pending_results: dict[str, dict[str, Any]] = {}

    def flush_tools() -> None:
        nonlocal pending_calls, pending_results
        if not pending_calls and not pending_results:
            return
        results = list(pending_results.values())
        # Keep result order aligned with call order when possible.
        ordered: list[dict[str, Any]] = []
        seen: set[str] = set()
        for call in pending_calls:
            cid = str(call.get("id") or "")
            if cid and cid in pending_results:
                ordered.append(pending_results[cid])
                seen.add(cid)
        for cid, res in pending_results.items():
            if cid not in seen:
                ordered.append(res)
        events.append(
            UiTranscriptEvent(
                kind="tools",
                tool_calls=list(pending_calls),
                tool_results=ordered or results,
            )
        )
        pending_calls = []
        pending_results = {}

    for msg in messages or []:
        role = _message_role(msg)
        if role in {"human", "user"}:
            flush_tools()
            # Context-compaction wrappers are for the model only.
            if is_lc_summarization_message(msg):
                continue
            # Mid-run steer is model-only chrome; never paint in the transcript.
            try:
                from synapse.steer import is_steer_message

                if is_steer_message(msg):
                    continue
            except Exception:  # noqa: BLE001
                pass
            text = _message_content(msg).strip()
            if is_context_compact_text(text):
                continue
            try:
                from synapse.steer import is_steer_message as _is_steer

                if _is_steer(text=text):
                    continue
            except Exception:  # noqa: BLE001
                pass
            images = _message_images(msg)
            if text or images:
                events.append(
                    UiTranscriptEvent(
                        kind="user",
                        text=text or ("(image)" if images else ""),
                        images=images,
                    )
                )
            continue

        if role == "system":
            continue

        if role == "tool":
            cid = str(
                getattr(msg, "tool_call_id", None)
                or (msg.get("tool_call_id") if isinstance(msg, dict) else None)
                or ""
            )
            name = str(
                getattr(msg, "name", None)
                or (msg.get("name") if isinstance(msg, dict) else None)
                or "tool"
            )
            content = _message_content(msg)
            status = "error" if _looks_error(content) else "ok"
            key = cid or f"anon-{len(pending_results)}"
            pending_results[key] = {
                "id": key,
                "name": name,
                "content": content,
                "status": status,
            }
            continue

        if role in {"ai", "assistant"}:
            reasoning = _message_reasoning(msg).strip()
            text = _message_content(msg).strip()
            calls = _tool_calls(msg)
            if reasoning:
                # Thought before tools/answer for this model turn.
                events.append(UiTranscriptEvent(kind="thought", text=reasoning))
            if calls:
                pending_calls.extend(calls)
            if text and not _looks_like_tool_payload(text):
                if is_lc_summarization_message(msg) or is_context_compact_text(text):
                    continue
                flush_tools()
                events.append(UiTranscriptEvent(kind="answer", text=text))
            continue

        # Unknown role: ignore noise
        continue

    flush_tools()
    return events


def _looks_error(content: str) -> bool:
    low = (content or "").casefold()
    return low.startswith("error") or "traceback" in low or "exception:" in low


def export_transcript_markdown(
    *,
    thread_id: str,
    title: str | None = None,
    model: str | None = None,
    messages: list[Any] | None = None,
) -> str:
    lines = [
        f"# {title or thread_id}",
        "",
        f"- thread_id: `{thread_id}`",
        f"- model: `{model or '-'}`",
        f"- messages: {len(messages or [])}",
        "",
        "## Transcript",
        "",
    ]
    if not messages:
        lines.append("(no checkpoint messages found)")
        lines.append("")
        return "\n".join(lines)

    for i, msg in enumerate(messages, 1):
        item = message_to_export_dict(msg)
        role = item.get("role") or "unknown"
        content = (item.get("content") or "").rstrip()
        lines.append(f"### {i}. {role}")
        lines.append("")
        lines.append(content if content else "(empty)")
        lines.append("")
    return "\n".join(lines)


def export_transcript_json(
    *,
    thread_id: str,
    title: str | None = None,
    model: str | None = None,
    messages: list[Any] | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "thread_id": thread_id,
        "title": title,
        "model": model,
        "meta": meta or {},
        "messages": [message_to_export_dict(m) for m in (messages or [])],
    }


def split_messages_by_turns(messages: list[Any]) -> list[list[Any]]:
    """按 HumanMessage 边界切分为轮次列表。

    每轮 = [user_msg, *后续非 human 消息]，以 human/user 为边界切分。
    入参为空或无 human 消息时返回空列表。
    """
    if not messages:
        return []
    turns: list[list[Any]] = []
    current: list[Any] = []
    for msg in messages:
        role = _message_role(msg)
        if role in {"human", "user"}:
            if current:
                turns.append(current)
            current = [msg]
        else:
            if current:
                current.append(msg)
    if current:
        turns.append(current)
    return turns


def format_turns_as_text(
    turns: list[list[Any]],
    *,
    max_turns: int = 0,
    max_chars_per_turn: int = 8000,
) -> str:
    """将轮次列表格式化为可读文本。

    Args:
        turns: split_messages_by_turns 的输出
        max_turns: 0 = 全量，N = 仅最后 N 轮
        max_chars_per_turn: 每轮最大字符数，超出截断
    """
    if not turns:
        return "(无对话内容)"

    target = turns
    if max_turns > 0:
        target = turns[-max_turns:]

    lines: list[str] = []
    total_turns = len(turns)
    if 0 < max_turns < total_turns:
        lines.append(f"[共 {total_turns} 轮，以下为最后 {max_turns} 轮]\n")

    for i, turn in enumerate(target):
        turn_idx = total_turns - len(target) + i + 1 if max_turns and max_turns < total_turns else i + 1
        lines.append(f"--- 第 {turn_idx} 轮 ---")
        for msg in turn:
            item = message_to_export_dict(msg)
            role = item["role"].upper()
            content = (item.get("content") or "").strip()
            if not content:
                continue
            if len(content) > max_chars_per_turn:
                content = content[:max_chars_per_turn] + "\n...[截断]..."
            lines.append(f"[{role}] {content}")
        lines.append("")
    return "\n".join(lines)


def parse_conversation_history_md(
    path: Path | str,
) -> list[dict[str, Any]]:
    """解析 deepagents 压缩导出的 conversation_history Markdown 文件。

    返回消息 dict 列表，格式与 message_to_export_dict 兼容，
    可直接传给 split_messages_by_turns / format_turns_as_text。

    文件格式：
        <message type="human">用户文本</message>
        <message type="ai">
          可选文本
          <tool_call id=".." name="..">{json_args}</tool_call>
        </message>
        <message type="tool">工具返回内容</message>
    """
    import re

    file_path = Path(path).expanduser()
    if not file_path.is_file():
        return []

    text = file_path.read_text(encoding="utf-8", errors="replace")

    # 按 <message type=...> 切分
    msg_pattern = re.compile(
        r"<message\s+type=\"(human|ai|tool)\">(.*?)</message>",
        re.DOTALL,
    )
    tool_call_pattern = re.compile(
        r'<tool_call\s+id="([^"]+)"\s+name="([^"]+)">(.*?)</tool_call>',
        re.DOTALL,
    )

    messages: list[dict[str, Any]] = []
    for m in msg_pattern.finditer(text):
        mtype = m.group(1)
        body = m.group(2)

        if mtype == "ai":
            # 提取 tool_calls 标签，其余为文本
            tool_calls: list[dict[str, Any]] = []
            parts: list[str] = []
            last_end = 0
            for tc in tool_call_pattern.finditer(body):
                prefix = body[last_end : tc.start()].strip()
                if prefix:
                    parts.append(prefix)
                args_str = tc.group(3).strip()
                try:
                    import json as _json
                    args = _json.loads(args_str)
                except Exception:  # noqa: BLE001
                    args = {"raw": args_str}
                tool_calls.append({
                    "id": tc.group(1),
                    "name": tc.group(2),
                    "args": args,
                })
                last_end = tc.end()
            suffix = body[last_end:].strip()
            if suffix:
                parts.append(suffix)
            content = "\n".join(parts)
            msg = {"role": "ai", "content": content}
            if tool_calls:
                msg["tool_calls"] = tool_calls
        elif mtype == "tool":
            msg = {"role": "tool", "content": body.strip()}
        else:
            msg = {"role": "human", "content": body.strip()}

        messages.append(msg)

    return messages
