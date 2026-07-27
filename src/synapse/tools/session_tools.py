"""跨会话引用工具 —— 让 Agent 能查阅其他会话的对话历史。

通过工厂函数 ``build_session_tools`` 创建工具，注入 SessionStore 和
checkpoint 路径依赖。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from synapse.tool_results import ToolResultStore


def build_tool_result_reader_tool(tool_results_path: Path | str) -> Any:
    """Create the guarded result reader shared by the main agent and subagents."""
    results = ToolResultStore(tool_results_path)

    @tool
    def read_tool_result(
        runtime: ToolRuntime,
        ref: str,
        offset: int = 0,
        limit: int = 200,
    ) -> str:
        """按行读取当前会话已归档的大型工具结果。

        仅在工具返回的 ``tool-result://...`` 引用明确需要更多细节时调用。
        不接受文件路径，避免读取会话目录外的任意文件。使用 offset/limit 分页，
        不要一次取回整个大型结果。

        Args:
            ref: 工具结果返回中的 ``tool-result://`` 引用。
            offset: 起始行号（0-indexed）。
            limit: 最多读取的行数，默认 200，最大 500。
        """
        config = dict(getattr(runtime, "config", None) or {})
        configurable = dict(config.get("configurable") or {})
        thread_id = str(configurable.get("thread_id") or "")
        # ToolRuntime is always injected by ToolNode. The fallback keeps direct
        # unit/tool invocation usable without weakening graph-time isolation.
        record = results.get(ref, expected_thread_id=thread_id or None)
        if record is None:
            return "工具结果引用未找到、已损坏或无权读取。"
        start = max(0, int(offset))
        count = min(500, max(1, int(limit)))
        lines = record.content.splitlines()
        selected = lines[start : start + count]
        body = "\n".join(selected) or "(empty result)"
        end = start + len(selected)
        suffix = ""
        if end < len(lines):
            suffix = f"\n\n[还有 {len(lines) - end} 行，使用 offset={end} 继续读取]"
        return (
            f"工具: {record.tool_name}\n"
            f"状态: {record.status}\n"
            f"引用: {record.ref}\n"
            f"行: {start}-{max(start, end - 1)} / {max(0, len(lines) - 1)}\n"
            f"{'─' * 40}\n{body}{suffix}"
        )

    return read_tool_result


def build_session_tools(
    sessions_path: Path | str,
    checkpoint_path: Path | str,
    tool_results_path: Path | str | None = None,
) -> list[Any]:
    """创建会话查阅工具列表。

    Args:
        sessions_path: sessions.sqlite 路径
        checkpoint_path: checkpoints.sqlite 路径
    Returns:
        [list_sessions, read_session]
    """
    from synapse.sessions import SessionStore, format_session_table

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

    result_root = tool_results_path or (Path(sessions_path).parent / "tool-results")
    return [list_sessions, read_session, build_tool_result_reader_tool(result_root)]
