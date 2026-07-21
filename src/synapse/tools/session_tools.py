"""跨会话引用工具 —— 让 Agent 能查阅其他会话的对话历史。

通过工厂函数 ``build_session_tools`` 创建工具，注入 SessionStore 和
checkpoint 路径依赖。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from langchain_core.tools import tool


def build_session_tools(
    sessions_path: Path | str,
    checkpoint_path: Path | str,
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
        from synapse.sessions import SessionStore
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
            header = (
                f"会话: {info.thread_id}\n"
                f"标题: {info.title}\n"
                f"模型: {bind.display()}\n"
                f"轮次: {len(turns)}（显示 {min(max_turns, len(turns)) if max_turns else len(turns)} 轮）\n"
                f"创建: {info.created_at}  更新: {info.updated_at}\n"
                f"{'─' * 40}\n\n"
            )
            return header + body
        return body

    return [list_sessions, read_session]
