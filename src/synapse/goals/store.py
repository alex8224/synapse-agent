"""Goal 持久化层：SQLite ``thread_goals`` 表。

表结构与 Codex ``thread_goals`` 对齐，存放在 sessions 数据库
（``SessionStore`` 同一文件），每 thread 至多一行。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from synapse.goals.model import ThreadGoal, ThreadGoalStatus, new_goal_id

_SCHEMA = """
CREATE TABLE IF NOT EXISTS thread_goals (
    thread_id TEXT PRIMARY KEY NOT NULL,
    goal_id TEXT NOT NULL,
    objective TEXT NOT NULL,
    status TEXT NOT NULL,
    token_budget INTEGER,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    time_used_seconds INTEGER NOT NULL DEFAULT 0,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
)
"""


class GoalStoreError(Exception):
    """Goal 持久化操作失败。"""


class GoalStore:
    """SQLite-backed per-thread goal storage（线程安全，懒创建）。"""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    # ------------------------------------------------------------------
    # 基础读写
    # ------------------------------------------------------------------
    def get(self, thread_id: str) -> ThreadGoal | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM thread_goals WHERE thread_id = ?", (thread_id,)
            ).fetchone()
        return _goal_from_row(row) if row is not None else None

    def insert(
        self,
        thread_id: str,
        objective: str,
        *,
        status: ThreadGoalStatus = ThreadGoalStatus.ACTIVE,
        token_budget: int | None = None,
        goal_id: str | None = None,
    ) -> ThreadGoal | None:
        """插入新 goal。已有未完成 goal（active/paused/budget_limited）时返回 None。"""
        now_ms = int(time.time() * 1000)
        with self._lock:
            existing = self._conn.execute(
                "SELECT status FROM thread_goals WHERE thread_id = ?", (thread_id,)
            ).fetchone()
            if existing is not None:
                cur = ThreadGoalStatus(str(existing["status"]))
                if not cur.is_replaceable():
                    return None
                # 已完成/受阻/用量受限的旧目标被新目标替换。
                self._conn.execute("DELETE FROM thread_goals WHERE thread_id = ?", (thread_id,))
            goal = ThreadGoal(
                thread_id=thread_id,
                goal_id=goal_id or new_goal_id(),
                objective=objective,
                status=status,
                token_budget=token_budget,
                created_at_ms=now_ms,
                updated_at_ms=now_ms,
            )
            self._conn.execute(
                """
                INSERT INTO thread_goals (
                    thread_id, goal_id, objective, status, token_budget,
                    tokens_used, time_used_seconds, created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, 0, 0, ?, ?)
                """,
                (
                    goal.thread_id,
                    goal.goal_id,
                    goal.objective,
                    goal.status.value,
                    goal.token_budget,
                    goal.created_at_ms,
                    goal.updated_at_ms,
                ),
            )
            self._conn.commit()
        return goal

    def update(
        self,
        thread_id: str,
        *,
        objective: str | None = None,
        status: ThreadGoalStatus | None = None,
        token_budget: int | None | object = ...,
        expected_goal_id: str | None = None,
        expected_status: ThreadGoalStatus | None = None,
    ) -> ThreadGoal | None:
        """部分更新。token_budget 默认（``...``）表示不修改；传 None 表示清空。

        expected_goal_id / expected_status 用于并发防护：不匹配时返回 None。
        """
        fields: list[str] = []
        values: list[Any] = []
        if objective is not None:
            fields.append("objective = ?")
            values.append(objective)
        if status is not None:
            fields.append("status = ?")
            values.append(status.value)
        if token_budget is not ...:
            fields.append("token_budget = ?")
            values.append(token_budget)
        if not fields:
            return self.get(thread_id)
        fields.append("updated_at_ms = ?")
        values.append(int(time.time() * 1000))
        where = "WHERE thread_id = ?"
        values.append(thread_id)
        if expected_goal_id is not None:
            where += " AND goal_id = ?"
            values.append(expected_goal_id)
        if expected_status is not None:
            where += " AND status = ?"
            values.append(expected_status.value)

        with self._lock:
            row = self._conn.execute(
                f"UPDATE thread_goals SET {', '.join(fields)} {where}",
                tuple(values),
            )
            self._conn.commit()
            if row.rowcount == 0:
                return None
            fresh = self._conn.execute(
                "SELECT * FROM thread_goals WHERE thread_id = ?", (thread_id,)
            ).fetchone()
        return _goal_from_row(fresh) if fresh is not None else None

    def clear(self, thread_id: str) -> ThreadGoal | None:
        """删除并返回被清除的 goal。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM thread_goals WHERE thread_id = ?", (thread_id,)
            ).fetchone()
            self._conn.execute("DELETE FROM thread_goals WHERE thread_id = ?", (thread_id,))
            self._conn.commit()
        return _goal_from_row(row) if row is not None else None

    # ------------------------------------------------------------------
    # 用量记账（token + 时间），与 Codex ``account_thread_goal_usage`` 对齐
    # ------------------------------------------------------------------
    def account_usage(
        self,
        thread_id: str,
        *,
        time_delta_seconds: int,
        token_delta: int,
        allow_complete: bool = False,
        allow_stopped: bool = False,
        expected_goal_id: str | None = None,
    ) -> ThreadGoal | None:
        """累加时间/token 用量，预算耗尽时自动置为 budget_limited。

        仅对 active（或按参数放行 complete/stopped 状态）的 goal 记账。
        返回更新后的 goal；无匹配 goal 或无可记用量时返回 None。
        """
        time_delta_seconds = max(0, int(time_delta_seconds))
        token_delta = max(0, int(token_delta))
        if time_delta_seconds == 0 and token_delta == 0:
            return self.get(thread_id)

        statuses = ["active", "budget_limited"]
        if allow_complete:
            statuses.append("complete")
        if allow_stopped:
            statuses.extend(["paused", "blocked", "usage_limited"])
        placeholders = ",".join("?" for _ in statuses)
        now_ms = int(time.time() * 1000)

        sql = f"""
            UPDATE thread_goals
            SET
                time_used_seconds = time_used_seconds + ?,
                tokens_used = tokens_used + ?,
                status = CASE
                    WHEN status = 'active'
                         AND token_budget IS NOT NULL
                         AND tokens_used + ? >= token_budget
                    THEN 'budget_limited'
                    ELSE status
                END,
                updated_at_ms = ?
            WHERE thread_id = ? AND status IN ({placeholders})
        """
        params: list[Any] = [
            time_delta_seconds,
            token_delta,
            token_delta,
            now_ms,
            thread_id,
            *statuses,
        ]
        if expected_goal_id is not None:
            sql += " AND goal_id = ?"
            params.append(expected_goal_id)

        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            if cur.rowcount == 0:
                return None
            row = self._conn.execute(
                "SELECT * FROM thread_goals WHERE thread_id = ?", (thread_id,)
            ).fetchone()
        return _goal_from_row(row) if row is not None else None


def _goal_from_row(row: sqlite3.Row) -> ThreadGoal:
    return ThreadGoal(
        thread_id=str(row["thread_id"]),
        goal_id=str(row["goal_id"]),
        objective=str(row["objective"]),
        status=ThreadGoalStatus(str(row["status"])),
        token_budget=row["token_budget"],
        tokens_used=int(row["tokens_used"] or 0),
        time_used_seconds=int(row["time_used_seconds"] or 0),
        created_at_ms=int(row["created_at_ms"] or 0),
        updated_at_ms=int(row["updated_at_ms"] or 0),
    )
