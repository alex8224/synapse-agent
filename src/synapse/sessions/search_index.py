"""会话消息全文索引 —— 普通 SQLite 表 + LIKE 的惰性增量索引。

消息本体存储在 checkpoints.sqlite 的 writes 增量里（delta 存储），每次搜索
全量重建代价过高。本模块维护一个轻量索引：

- ``messages`` 表：每条消息一行（thread_id, seq, role, content），content 截断；
- ``indexed`` 表：记录每个会话已索引到的最新 checkpoint_id。

搜索时只重建"最新 checkpoint 落后"的会话（惰性增量），再在索引表上 LIKE。
中文不依赖 FTS5 分词，直接子串匹配与 store.search 语义一致。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from synapse.sessions.store import SessionStore

# 单条消息索引的最大字符数（工具输出等内容很大，控制索引体积）
MAX_CONTENT_CHARS = 2000
# 搜索命中片段的最大字符数
SNIPPET_CHARS = 120
# 索引 schema 版本；索引策略变化（如过滤 tool 消息）时递增以触发全量重建
SCHEMA_VERSION = 2


def default_search_index_path(sessions_path: Path | str) -> Path:
    """索引库默认与 sessions.sqlite 同目录。"""
    return Path(sessions_path).expanduser().resolve().parent / "search-index.sqlite"


class SessionSearchIndex:
    """惰性增量消息全文索引。"""

    def __init__(
        self,
        db_path: Path | str,
        *,
        store: SessionStore | None = None,
        checkpoint_path: Path | str | None = None,
    ) -> None:
        self.path = Path(db_path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # 多进程/多实例（TUI、CLI、子 agent）可能同时同步索引：
        # WAL 允许读写并发，busy_timeout 让写锁竞争等待而非立即报 locked。
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:  # noqa: BLE001
            pass
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._init_db()
        self._store = store
        self._checkpoint_path = Path(checkpoint_path).expanduser() if checkpoint_path else None
        self._ckpt_conn: sqlite3.Connection | None = None
        self._saver: Any | None = None

    def _init_db(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS indexed (
                thread_id TEXT PRIMARY KEY,
                last_checkpoint_id TEXT,
                updated_at TEXT
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                thread_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                PRIMARY KEY (thread_id, seq)
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"
        )
        # 索引策略变化时（SCHEMA_VERSION 递增）清空旧索引，下一次 sync 全量重建。
        # 版本缺失（新库或旧版索引）同样视为需重建，避免旧 tool 行残留。
        # 并发实例可能同时迁移：捕获写冲突，交由 sync 的幂等重建处理。
        try:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is not None and str(row["value"]) == str(SCHEMA_VERSION):
                self._conn.commit()
                return
            self._conn.execute("DELETE FROM messages")
            self._conn.execute("DELETE FROM indexed")
            self._conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            self._conn.commit()
        except sqlite3.Error:  # noqa: BLE001  -- 并发迁移竞争，静默降级
            try:
                self._conn.rollback()
            except sqlite3.Error:
                pass

    def close(self) -> None:
        if self._ckpt_conn is not None:
            self._ckpt_conn.close()
            self._ckpt_conn = None
            self._saver = None
        self._conn.close()

    # -- 增量同步 -----------------------------------------------------------

    def sync(self, thread_ids: Iterable[str], *, max_sync: int = 50) -> int:
        """同步索引到最新 checkpoint，返回实际重建的会话数。

        只重建最新 checkpoint_id 与索引记录不一致的会话；每轮最多处理
        ``max_sync`` 个（调用方按最近更新排序传入），避免单次调用过慢。
        并发实例同时写入索引时可能触发 SQLite 锁竞争：这里降级为跳过本次
        同步（保留已有索引），由调用方继续搜索，不抛异常。
        """
        tids = [t for t in thread_ids if t]
        if not tids or self._checkpoint_path is None:
            return 0
        try:
            latest = self._latest_checkpoint_ids(tids)
        except sqlite3.Error:  # noqa: BLE001  -- checkpoint 库并发读失败，降级
            return 0
        if not latest:
            return 0
        synced = 0
        for tid in tids:
            if synced >= max_sync:
                break
            cid = latest.get(tid)
            if cid is None:
                continue
            try:
                if self._get_last_indexed(tid) == cid:
                    continue
                messages = self._rebuild_messages(tid)
                updated_at = ""
                if self._store is not None:
                    info = self._store.get(tid)
                    updated_at = info.updated_at if info else ""
                self._replace_thread(tid, cid, messages, updated_at)
                synced += 1
            except sqlite3.Error:  # noqa: BLE001  -- 写锁竞争，跳过该会话
                continue
        return synced

    def indexed_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM indexed").fetchone()
        return int(row["n"])

    def _get_last_indexed(self, thread_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT last_checkpoint_id FROM indexed WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
        return row["last_checkpoint_id"] if row else None

    def _latest_checkpoint_ids(self, thread_ids: list[str]) -> dict[str, str]:
        path = self._checkpoint_path
        if path is None or not path.is_file():
            return {}
        conn = sqlite3.connect(str(path), check_same_thread=False)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=10000")
            placeholders = ",".join("?" * len(thread_ids))
            rows = conn.execute(
                f"SELECT thread_id, MAX(checkpoint_id) AS cid FROM checkpoints "
                f"WHERE checkpoint_ns = '' AND thread_id IN ({placeholders}) "
                f"GROUP BY thread_id",
                list(thread_ids),
            ).fetchall()
            return {str(r["thread_id"]): str(r["cid"]) for r in rows}
        finally:
            conn.close()

    def _rebuild_messages(self, thread_id: str) -> list[Any]:
        from langgraph.checkpoint.sqlite import SqliteSaver

        from synapse.sessions.transcript import load_messages_from_checkpointer

        if self._saver is None:
            self._ckpt_conn = sqlite3.connect(str(self._checkpoint_path), check_same_thread=False)
            # 运行时 AsyncSqliteSaver 可能正在写 checkpoint 库（WAL），
            # 只读连接加 busy_timeout 避免读锁等待报错。
            self._ckpt_conn.execute("PRAGMA busy_timeout=10000")
            self._saver = SqliteSaver(self._ckpt_conn)
        return load_messages_from_checkpointer(self._saver, thread_id)

    def _replace_thread(
        self,
        thread_id: str,
        checkpoint_id: str,
        messages: list[Any],
        updated_at: str,
    ) -> None:
        rows: list[tuple[str, int, str, str]] = []
        for seq, msg in enumerate(messages):
            if isinstance(msg, dict):
                role = str(msg.get("type") or msg.get("role") or "unknown")
                content = msg.get("content")
            else:
                role = str(getattr(msg, "type", "unknown"))
                content = getattr(msg, "content", "")
            # 只索引对话文本；工具调用与系统消息不参与全文搜索。
            if role in {"tool", "system"}:
                continue
            text = _content_to_text(content)
            if not text:
                continue
            rows.append((thread_id, seq, role, text[:MAX_CONTENT_CHARS]))
        with self._conn:
            self._conn.execute("DELETE FROM messages WHERE thread_id = ?", (thread_id,))
            self._conn.executemany(
                "INSERT INTO messages(thread_id, seq, role, content) VALUES (?, ?, ?, ?)",
                rows,
            )
            self._conn.execute(
                "INSERT OR REPLACE INTO indexed(thread_id, last_checkpoint_id, updated_at)"
                " VALUES (?, ?, ?)",
                (thread_id, checkpoint_id, updated_at),
            )

    # -- 搜索 ---------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        limit: int = 50,
        roles: tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]:
        """在索引消息中按关键字子串匹配，按会话更新时间倒序返回。

        roles: 限定消息角色（如 ("human", "ai")）；None 表示全部。
        """
        q = f"%{query.strip()}%"
        sql = (
            "SELECT m.thread_id, m.seq, m.role, m.content, i.updated_at "
            "FROM messages m JOIN indexed i ON i.thread_id = m.thread_id "
            "WHERE m.content LIKE ?"
        )
        params: list[Any] = [q]
        if roles:
            sql += f" AND m.role IN ({','.join('?' * len(roles))})"
            params.extend(roles)
        sql += " ORDER BY i.updated_at DESC, m.seq LIMIT ?"
        params.append(max(1, limit))
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def _content_to_text(content: Any) -> str:
    """把消息 content 规整为可搜索文本（多模态块只取 text）。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
            elif isinstance(block, str) and block.strip():
                parts.append(block.strip())
        return "\n".join(parts).strip()
    return str(content).strip()
