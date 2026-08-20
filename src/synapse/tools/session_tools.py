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

from synapse.tool_output.repository import ToolOutputRepository


def _format_search_results(
    store: Any,
    meta_hits: list[Any],
    msg_hits: list[dict[str, Any]],
    *,
    limit: int,
) -> str:
    """合并元数据命中与消息命中，按会话更新时间倒序输出。

    Args:
        store: SessionStore 实例
        meta_hits: store.search 的会话列表（标题/摘要/模型命中）
        msg_hits: search_index.search 的命中行（含 thread_id/role/content）
        limit: 最多输出的会话数
    """
    from synapse.sessions.search_index import SNIPPET_CHARS

    meta_by_id = {s.thread_id: s for s in meta_hits}
    hits_by_thread: dict[str, list[dict[str, Any]]] = {}
    for hit in msg_hits:
        hits_by_thread.setdefault(str(hit["thread_id"]), []).append(hit)

    ordered: list[str] = list(meta_by_id)
    for tid in hits_by_thread:
        if tid not in meta_by_id:
            ordered.append(tid)

    def _sort_key(tid: str) -> str:
        info = store.get(tid)
        return info.updated_at if info else ""

    ordered.sort(key=_sort_key, reverse=True)
    if not ordered:
        return "(没有找到匹配的会话记录)"

    lines = [f"共 {len(ordered)} 个命中会话，显示前 {min(limit, len(ordered))} 个："]
    for tid in ordered[:limit]:
        info = store.get(tid)
        title = (info.title if info else tid)[:80]
        updated = info.updated_at if info else ""
        lines.append(f"● {title}  [{tid}]  {updated}")
        if tid in meta_by_id:
            lines.append("    元数据命中（标题/摘要/模型/thread_id）")
        for hit in hits_by_thread.get(tid, [])[:2]:
            snippet = str(hit.get("content") or "").replace("\n", " ")[:SNIPPET_CHARS]
            lines.append(f"    - [{hit.get('role')}] {snippet}")
    return "\n".join(lines)


def build_tool_result_reader_tool(tool_output_db_path: Path | str) -> Any:
    """Deprecated: create the guarded reader for reversible transformed output.

    Kept for compatibility; no longer registered since the transform middleware
    that produces ``tool-output://`` references is not wired up.
    """
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
        [search_session, read_session]
    """
    from synapse.sessions.search_index import (
        SessionSearchIndex,
        default_search_index_path,
    )
    from synapse.sessions.store import SessionStore, format_session_table

    store = SessionStore(sessions_path)
    ckpt = Path(checkpoint_path)
    index = SessionSearchIndex(
        default_search_index_path(sessions_path),
        store=store,
        checkpoint_path=ckpt,
    )

    @tool
    def search_session(
        query: str = "",
        limit: int = 50,
        offset: int = 0,
        include_summary: bool = False,
        max_sync: int = 50,
    ) -> str:
        """搜索本地会话：按关键字匹配标题/摘要/模型，以及会话消息全文。

        **默认禁止调用**。仅当用户明确要求查阅、搜索、对比其他会话时使用
        （例如“列出最近会话”“找之前那个 bug 讨论”“看看某某会话”）。
        闲聊、问候、普通编码/排障、意图不明、仅为“多了解上下文”时一律不要调用。

        query 为空时返回最近会话列表。query 非空时同时匹配：
        1. 会话元数据（标题/摘要/模型/thread_id）；
        2. 会话消息全文（human/ai 文本，经本地增量索引，首次调用会先建立索引，
           可能需要数秒）。
        返回命中会话及其消息片段，获取完整内容请用 read_session。

        Args:
            query: 搜索关键字。为空时列出最近会话。
            limit: 最大返回会话数，默认 50。
            offset: 分页，跳过前 offset 条（按最近更新排序，0 = 不跳过）。
            include_summary: 是否附带会话摘要行（query 为空时生效）。
            max_sync: 本次最多同步多少会话的消息索引（0 = 只搜元数据）。
        """
        take = max(1, limit)
        total = offset + take

        if not query.strip():
            items = store.list_nonempty(limit=total)
            if offset:
                items = items[offset:]
            items = items[:take]
            if not items:
                return "(没有找到匹配的会话记录)"
            return format_session_table(items, include_summary=include_summary)

        meta = store.search(query, limit=total)

        msg_hits: list[dict[str, Any]] = []
        if max_sync > 0:
            recent = store.list_nonempty(limit=total + 200)
            index.sync([s.thread_id for s in recent], max_sync=max_sync)
            msg_hits = index.search(query, limit=200, roles=("human", "ai"))

        return _format_search_results(store, meta, msg_hits, limit=take)

    @tool
    def read_session(
        thread_id: str,
        max_turns: int = 0,
        include_summary: bool = True,
        include_tools: bool = False,
        offset: int = 0,
        limit: int = 0,
    ) -> str:
        """读取指定会话的对话历史内容。

        **默认禁止调用**。仅当用户明确要求读取某个会话内容时使用
        （通常先由 search_session 得到 thread_id，或用户直接给出会话 ID）。
        闲聊、问候、普通任务、未指明需要跨会话上下文时不要主动调用。

        按轮次切分对话，每轮 = 一条用户消息 + 后续 AI/工具消息。
        默认只保留用户与 AI 的文本内容（去掉工具调用与工具返回），
        需要查看工具细节时传 include_tools=True。

        Args:
            thread_id: 会话 ID（通过 search_session 获取）。
            max_turns: 返回最近 N 轮。0 表示返回全部轮次。
            include_summary: 是否在开头附带会话元信息。
            include_tools: 是否保留工具调用与工具返回消息（默认去掉）。
            offset: 分页，跳过前 offset 轮（0 = 不跳过）。
            limit: 分页，最多返回 limit 轮（0 = 全部）。
        """
        from synapse.sessions.transcript import (
            format_turns_as_text,
            load_messages_from_sqlite_file,
            split_messages_by_turns,
        )

        info = store.get(thread_id)
        if info is None:
            return (
                f"会话未找到: {thread_id}\n"
                f"提示：使用 search_session 查看可用会话列表，"
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

        if not include_tools:
            messages = [
                m
                for m in messages
                if not (m.get("type") if isinstance(m, dict) else getattr(m, "type", None))
                == "tool"
            ]

        turns = split_messages_by_turns(messages)
        body = format_turns_as_text(
            turns, max_turns=max_turns, offset=offset, limit=limit
        )

        if include_summary:
            bind = info.binding()
            shown = len(turns)
            if max_turns > 0:
                shown = min(max_turns, shown)
            if limit > 0:
                shown = min(limit, max(0, shown - offset))
            header = (
                f"会话: {info.thread_id}\n"
                f"标题: {info.title}\n"
                f"模型: {bind.display()}\n"
                f"轮次: {len(turns)}（显示 {shown} 轮）\n"
                f"创建: {info.created_at}  更新: {info.updated_at}\n"
                f"{'─' * 40}\n\n"
            )
            return header + body
        return body

    # Deprecated: read_tool_result is no longer registered (the transform
    # middleware that produces tool-output:// references is not wired up).
    return [search_session, read_session]
