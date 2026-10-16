"""项目任务服务层：业务操作与跨层变更事件通知。

与 GoalService 类似，基于数据库文件路径进行分片缓存，
保证同进程内对同一项目 tasks.sqlite 的访问与监听同步。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path

from synapse.tasks.model import ProjectTask, TaskPriority, TaskStatus
from synapse.tasks.store import TaskStore

logger = logging.getLogger(__name__)

# 回调签名：callback(task: ProjectTask | None, action: str)
# action 取值: "create" | "update" | "delete"
TaskListener = Callable[[ProjectTask | None, str], None]


class ProjectTaskService:
    """管理项目待办事项的服务实例。"""

    def __init__(self, store: TaskStore) -> None:
        self.store = store
        self._listeners: list[TaskListener] = []
        self._lock = threading.RLock()

    def add_listener(self, listener: TaskListener) -> None:
        with self._lock:
            if listener not in self._listeners:
                self._listeners.append(listener)

    def remove_listener(self, listener: TaskListener) -> None:
        with self._lock:
            self._listeners = [cb for cb in self._listeners if cb is not listener]

    def notify(self, task: ProjectTask | None, action: str) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(task, action)
            except Exception:
                logger.debug("Task listener failed for action %s", action, exc_info=True)

    def list_tasks(
        self,
        *,
        status: TaskStatus | None = None,
        label: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ProjectTask]:
        return self.store.list(status=status, label=label, limit=limit, offset=offset)

    def get_task(self, task_id: str) -> ProjectTask | None:
        return self.store.get(task_id)

    def create_task(
        self,
        title: str,
        description: str = "",
        *,
        priority: TaskPriority | str = TaskPriority.MEDIUM,
        status: TaskStatus | str = TaskStatus.TODO,
        labels: list[str] | None = None,
        assigned_thread_id: str | None = None,
    ) -> ProjectTask:
        task = self.store.insert(
            title=title,
            description=description,
            priority=priority,
            status=status,
            labels=labels,
            assigned_thread_id=assigned_thread_id,
        )
        self.notify(task, "create")
        return task

    def update_task(
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
        task = self.store.update(
            task_id,
            title=title,
            description=description,
            status=status,
            priority=priority,
            labels=labels,
            assigned_thread_id=assigned_thread_id,
            clear_assigned_thread=clear_assigned_thread,
        )
        if task is not None:
            self.notify(task, "update")
        return task

    def delete_task(self, task_id: str) -> bool:
        task = self.store.get(task_id)
        if task is None:
            return False
        success = self.store.delete(task_id)
        if success:
            self.notify(task, "delete")
        return success


_SERVICES: dict[str, ProjectTaskService] = {}
_SERVICES_LOCK = threading.RLock()


def get_task_service(tasks_path: Path | str) -> ProjectTaskService:
    """获取指定路径的 ProjectTaskService 单例。"""
    key = str(Path(tasks_path).expanduser().resolve())
    with _SERVICES_LOCK:
        service = _SERVICES.get(key)
        if service is None:
            store = TaskStore(key)
            service = ProjectTaskService(store)
            _SERVICES[key] = service
        return service


def reset_task_service(tasks_path: Path | str | None = None) -> None:
    """清除缓存的 service 单例，用于测试与退出。"""
    with _SERVICES_LOCK:
        if tasks_path is None:
            for s in _SERVICES.values():
                s.store.close()
            _SERVICES.clear()
        else:
            key = str(Path(tasks_path).expanduser().resolve())
            s = _SERVICES.pop(key, None)
            if s is not None:
                s.store.close()
