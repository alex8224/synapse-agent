"""ProjectTask 持久化层：SQLite ``project_tasks`` 表。

默认保存在项目本地目录（如 ``<workspace>/.synapse/tasks.sqlite``）。
支持 WAL 并发读写、只读无副作用访问以及线程安全锁保护。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from synapse.tasks.model import (
    ProjectTask,
    TaskPriority,
    TaskStatus,
    new_task_id,
    validate_task_description,
    validate_task_labels,
    validate_task_priority,
    validate_task_status,
    validate_task_title,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS project_tasks (
    id TEXT PRIMARY KEY NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    priority TEXT NOT NULL,
    labels_json TEXT NOT NULL DEFAULT '[]',
    assigned_thread_id TEXT,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    completed_at_ms INTEGER
);

CREATE INDEX IF NOT EXISTS idx_project_tasks_status ON project_tasks (status);
CREATE INDEX IF NOT EXISTS idx_project_tasks_updated ON project_tasks (updated_at_ms DESC);
CREATE INDEX IF NOT EXISTS idx_project_tasks_assigned ON project_tasks (assigned_thread_id);
"""


class TaskStoreError(Exception):
    """Task 持久化异常。"""


def _task_from_row(row: sqlite3.Row) -> ProjectTask:
    try:
        labels = json.loads(row["labels_json"])
        if not isinstance(labels, list):
            labels = []
    except Exception:
        labels = []
    completed_at = row["completed_at_ms"]
    return ProjectTask(
        id=str(row["id"]),
        title=str(row["title"]),
        description=str(row["description"] or ""),
        status=TaskStatus(str(row["status"])),
        priority=TaskPriority(str(row["priority"])),
        labels=[str(x) for x in labels],
        assigned_thread_id=str(row["assigned_thread_id"]) if row["assigned_thread_id"] else None,
        created_at_ms=int(row["created_at_ms"]),
        updated_at_ms=int(row["updated_at_ms"]),
        completed_at_ms=int(completed_at) if completed_at is not None else None,
    )


def read_task_readonly(tasks_path: Path | str, task_id: str) -> ProjectTask | None:
    """纯只读查询单个任务，不自动建库/建表。"""
    if not isinstance(task_id, str) or not task_id:
        return None
    path = Path(tasks_path).expanduser()
    if not path.is_file():
        return None
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                "SELECT * FROM project_tasks WHERE id = ?", (task_id,)
            ).fetchone()
        except sqlite3.Error:
            return None
        return _task_from_row(row) if row is not None else None
    finally:
        connection.close()


def read_tasks_readonly(
    tasks_path: Path | str,
    *,
    status: TaskStatus | None = None,
    label: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[ProjectTask]:
    """纯只读查询任务列表，不自动建库/建表。"""
    path = Path(tasks_path).expanduser()
    if not path.is_file():
        return []
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        connection.row_factory = sqlite3.Row
        clauses: list[str] = []
        params: list[object] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = (
            f"SELECT * FROM project_tasks {where_clause} "
            "ORDER BY updated_at_ms DESC LIMIT ? OFFSET ?"
        )
        params.extend([max(1, min(limit, 500)), max(0, offset)])
        try:
            rows = connection.execute(query, params).fetchall()
        except sqlite3.Error:
            return []
        tasks = [_task_from_row(r) for r in rows]
        if label:
            target_label = label.strip().lower()
            tasks = [t for t in tasks if target_label in t.labels]
        return tasks
    finally:
        connection.close()


class TaskStore:
    """SQLite 支撑的项目任务持久化仓库（线程安全）。"""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=15000")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    def get(self, task_id: str) -> ProjectTask | None:
        if not task_id:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM project_tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return _task_from_row(row) if row is not None else None

    def list(
        self,
        *,
        status: TaskStatus | None = None,
        label: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ProjectTask]:
        clauses: list[str] = []
        params: list[object] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = (
            f"SELECT * FROM project_tasks {where_clause} "
            "ORDER BY updated_at_ms DESC LIMIT ? OFFSET ?"
        )
        params.extend([max(1, min(limit, 500)), max(0, offset)])
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        tasks = [_task_from_row(r) for r in rows]
        if label:
            target_label = label.strip().lower()
            tasks = [t for t in tasks if target_label in t.labels]
        return tasks

    def count(self, *, status: TaskStatus | None = None) -> int:
        clauses: list[str] = []
        params: list[object] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"SELECT COUNT(*) as c FROM project_tasks {where_clause}"
        with self._lock:
            row = self._conn.execute(query, params).fetchone()
        return int(row["c"]) if row is not None else 0

    def insert(
        self,
        title: str,
        description: str = "",
        *,
        priority: TaskPriority | str = TaskPriority.MEDIUM,
        status: TaskStatus | str = TaskStatus.TODO,
        labels: list[str] | None = None,
        assigned_thread_id: str | None = None,
        task_id: str | None = None,
    ) -> ProjectTask:
        clean_title = validate_task_title(title)
        clean_desc = validate_task_description(description)
        valid_status = validate_task_status(status)
        valid_priority = validate_task_priority(priority)
        clean_labels = validate_task_labels(labels)
        clean_assigned = str(assigned_thread_id).strip() if assigned_thread_id else None
        now_ms = int(time.time() * 1000)
        final_id = task_id.strip() if task_id and task_id.strip() else new_task_id()
        completed_at = now_ms if valid_status.is_terminal() else None

        task = ProjectTask(
            id=final_id,
            title=clean_title,
            description=clean_desc,
            status=valid_status,
            priority=valid_priority,
            labels=clean_labels,
            assigned_thread_id=clean_assigned,
            created_at_ms=now_ms,
            updated_at_ms=now_ms,
            completed_at_ms=completed_at,
        )

        with self._lock:
            self._conn.execute(
                """
                INSERT INTO project_tasks (
                    id, title, description, status, priority,
                    labels_json, assigned_thread_id, created_at_ms,
                    updated_at_ms, completed_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.id,
                    task.title,
                    task.description,
                    task.status.value,
                    task.priority.value,
                    json.dumps(task.labels),
                    task.assigned_thread_id,
                    task.created_at_ms,
                    task.updated_at_ms,
                    task.completed_at_ms,
                ),
            )
            self._conn.commit()
        return task

    def update(
        self,
        task_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
        status: TaskStatus | str | None = None,
        priority: TaskPriority | str | None = None,
        labels: list[str] | None = None,
        assigned_thread_id: str | None = None,
        clear_assigned_thread: bool = False,
    ) -> ProjectTask | None:
        with self._lock:
            current = self.get(task_id)
            if current is None:
                return None

            new_title = validate_task_title(title) if title is not None else current.title
            new_desc = (
                validate_task_description(description)
                if description is not None
                else current.description
            )
            new_status = (
                validate_task_status(status) if status is not None else current.status
            )
            new_priority = (
                validate_task_priority(priority) if priority is not None else current.priority
            )
            new_labels = validate_task_labels(labels) if labels is not None else current.labels

            if clear_assigned_thread:
                new_assigned = None
            elif assigned_thread_id is not None:
                new_assigned = str(assigned_thread_id).strip() or None
            else:
                new_assigned = current.assigned_thread_id

            now_ms = int(time.time() * 1000)
            new_completed_at = current.completed_at_ms
            if new_status.is_terminal() and not current.status.is_terminal():
                new_completed_at = now_ms
            elif not new_status.is_terminal() and current.status.is_terminal():
                new_completed_at = None

            self._conn.execute(
                """
                UPDATE project_tasks
                SET title = ?, description = ?, status = ?, priority = ?,
                    labels_json = ?, assigned_thread_id = ?, updated_at_ms = ?,
                    completed_at_ms = ?
                WHERE id = ?
                """,
                (
                    new_title,
                    new_desc,
                    new_status.value,
                    new_priority.value,
                    json.dumps(new_labels),
                    new_assigned,
                    now_ms,
                    new_completed_at,
                    task_id,
                ),
            )
            self._conn.commit()
            return self.get(task_id)

    def delete(self, task_id: str) -> bool:
        if not task_id:
            return False
        with self._lock:
            cursor = self._conn.execute("DELETE FROM project_tasks WHERE id = ?", (task_id,))
            self._conn.commit()
            return cursor.rowcount > 0
