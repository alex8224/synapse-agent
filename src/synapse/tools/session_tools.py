"""跨会话引用工具 —— 让 Agent 能查阅其他会话的对话历史。

通过工厂函数 ``build_session_tools`` 创建工具，注入 SessionStore 和
checkpoint 路径依赖。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from synapse.tool_output.pipeline import ToolOutputRepository


def build_tool_result_reader_tool(tool_output_db_path: Path | str) -> Any:
    """Create the guarded reader for reversible transformed output."""
    results = ToolOutputRepository(tool_output_db_path)

    @tool
    def read_tool_result(
        runtime: ToolRuntime,
        ref: str,
        offset: int = 0,
        limit: int = 200,
        query: str = "",
        max_results: int = 20,
        context_lines: int = 2,
    ) -> str:
        """读取当前会话被压缩工具输出的原文。

        支持精确分页，或通过 query 在原文中进行本地关键词召回。仅接受工具
        输出提供的 ``tool-output://...`` 引用，不能读取任意文件。

        Args:
            ref: 工具输出中的 ``tool-output://`` 引用。
            offset: 精确分页的起始行号（0-indexed）。
            limit: 精确分页行数，默认 200，最大 500。
            query: 可选关键词；提供后返回相关片段。
            max_results: 查询模式最多返回的命中行数，最大 50。
            context_lines: 每个命中的相邻上下文行数，最大 10。
        """
        config = dict(getattr(runtime, "config", None) or {})
        configurable = dict(config.get("configurable") or {})
        thread_id = str(configurable.get("thread_id") or "")
        record = results.get(ref, expected_thread_id=thread_id or None)
        if record is None:
            return "工具结果引用未找到、已损坏或无权读取。"
        started = time.perf_counter()
        if query.strip():
            matches = results.search(
                ref,
                query,
                expected_thread_id=thread_id or None,
                max_results=min(50, max(1, int(max_results))),
                context_lines=min(10, max(0, int(context_lines))),
            )
            if not matches:
                return f"工具: {record.tool_name}\n引用: {record.ref}\n(没有匹配 {query!r} 的行)"
            body = "\n".join(f"{line_no}: {line}" for line_no, line in matches)
            result = (
                f"工具: {record.tool_name}\n引用: {record.ref}\n查询: {query}\n{'─' * 40}\n{body}"
            )
            results.record_retrieval(
                thread_id=thread_id or record.thread_id,
                ref=ref,
                mode="query",
                returned_bytes=len(result.encode("utf-8")),
                duration_ms=(time.perf_counter() - started) * 1000,
            )
            return result
        start = max(0, int(offset))
        count = min(500, max(1, int(limit)))
        lines = record.content.splitlines()
        selected = lines[start : start + count]
        body = "\n".join(selected) or "(empty result)"
        end = start + len(selected)
        suffix = (
            f"\n\n[还有 {len(lines) - end} 行，使用 offset={end} 继续读取]"
            if end < len(lines)
            else ""
        )
        result = (
            f"工具: {record.tool_name}\n状态: {record.status}\n引用: {record.ref}\n"
            f"行: {start}-{max(start, end - 1)} / {max(0, len(lines) - 1)}\n"
            f"{'─' * 40}\n{body}{suffix}"
        )
        results.record_retrieval(
            thread_id=thread_id or record.thread_id,
            ref=ref,
            mode="pagination",
            returned_bytes=len(result.encode("utf-8")),
            duration_ms=(time.perf_counter() - started) * 1000,
        )
        return result

    return read_tool_result


def build_session_tools(
    sessions_path: Path | str,
    checkpoint_path: Path | str,
    tool_output_db_path: Path | str | None = None,
) -> list[Any]:
    """创建会话查阅工具列表。

    Args:
        sessions_path: sessions.sqlite 路径
        checkpoint_path: checkpoints.sqlite 路径
    Returns:
        [list_sessions, read_session]
    """
    from synapse.sessions.store import SessionStore, format_session_table

    store = SessionStore(sessions_path)
    ckpt = Path(checkpoint_path)

    @tool
    def list_sessions(query: str = "", limit: int = 20) -> str:
        """列出本地会话记录，支持按标题/ID 模糊搜索。

        **默认禁止调用**。仅当用户明确要求查阅、搜索、对比其他会话时使用
        （例如“列出最近会话”“找之前那个 bug 讨论”“看看某某会话”）。
        闲聊、问候、普通编码/排障、意图不明、仅为“多了解上下文”时一律不要调用。

        返回会话基本信息，不包含对话内容；获取对话内容请用 read_session。

        Args:
            query: 可选，按标题或 thread_id 搜索关键词。为空时返回最近会话。
            limit: 最大返回数，默认 20。
        """
        if query.strip():
            items = store.search(query, limit=limit)
        else:
            items = store.list_nonempty(limit=limit)
        if not items:
            return "(没有找到匹配的会话记录)"
        return format_session_table(items)

    @tool
    def read_session(
        thread_id: str,
        max_turns: int = 0,
        include_summary: bool = True,
    ) -> str:
        """读取指定会话的对话历史内容。

        **默认禁止调用**。仅当用户明确要求读取某个会话内容时使用
        （通常先由 list_sessions 得到 thread_id，或用户直接给出会话 ID）。
        闲聊、问候、普通任务、未指明需要跨会话上下文时不要主动调用。

        按轮次切分对话，每轮 = 一条用户消息 + 后续 AI/工具消息。
        可指定只取最后 N 轮，避免上下文过长。

        Args:
            thread_id: 会话 ID（通过 list_sessions 获取）。
            max_turns: 返回最近 N 轮。0 表示返回全部轮次。
            include_summary: 是否在开头附带会话元信息。
        """
        from synapse.transcript import (
            format_turns_as_text,
            load_messages_from_sqlite_file,
            split_messages_by_turns,
        )

        info = store.get(thread_id)
        if info is None:
            return (
                f"会话未找到: {thread_id}\n"
                f"提示：使用 list_sessions 查看可用会话列表，"
                f"确保 thread_id 完全匹配。"
            )

        messages = load_messages_from_sqlite_file(ckpt, thread_id)
        if not messages:
            return (
                f"会话 {thread_id} 没有对话记录。\n"
                f"标题: {info.title}\n"
                f"创建: {info.created_at}  更新: {info.updated_at}\n"
                f"模型: {info.binding().display()}"
            )

        turns = split_messages_by_turns(messages)
        body = format_turns_as_text(turns, max_turns=max_turns)

        if include_summary:
            bind = info.binding()
            turns_display = min(max_turns, len(turns)) if max_turns else len(turns)
            header = (
                f"会话: {info.thread_id}\n"
                f"标题: {info.title}\n"
                f"模型: {bind.display()}\n"
                f"轮次: {len(turns)}（显示 {turns_display} 轮）\n"
                f"创建: {info.created_at}  更新: {info.updated_at}\n"
                f"{'─' * 40}\n\n"
            )
            return header + body
        return body

    output_db = tool_output_db_path or (Path(sessions_path).parent / "tool-outputs.sqlite")
    return [list_sessions, read_session, build_tool_result_reader_tool(output_db)]
