"""项目级待办事项（Project Tasks）领域模型。

不同于单会话长程目标（thread_goals）和临时步骤清单（write_todos），
ProjectTask 是持久化在项目层级（.synapse/tasks.sqlite）的长期规划项。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

MAX_TASK_TITLE_CHARS = 200
MAX_TASK_DESCRIPTION_CHARS = 20_000
MAX_LABELS_COUNT = 20
MAX_LABEL_CHARS = 50


class TaskStatus(StrEnum):
    """待办事项生命周期状态。"""

    TODO = "todo"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    DONE = "done"

    def is_terminal(self) -> bool:
        return self is TaskStatus.DONE

    def label(self) -> str:
        return {
            TaskStatus.TODO: "To Do",
            TaskStatus.IN_PROGRESS: "In Progress",
            TaskStatus.BLOCKED: "Blocked",
            TaskStatus.DONE: "Done",
        }[self]


class TaskPriority(StrEnum):
    """待办事项优先级。"""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    def label(self) -> str:
        return {
            TaskPriority.LOW: "Low",
            TaskPriority.MEDIUM: "Medium",
            TaskPriority.HIGH: "High",
            TaskPriority.CRITICAL: "Critical",
        }[self]


def new_task_id() -> str:
    """生成前缀可识别的唯一任务 ID，例如 task-a1b2c3d4e5f6。"""
    return f"task-{uuid.uuid4().hex[:12]}"


def validate_task_title(title: Any) -> str:
    if not isinstance(title, str):
        raise ValueError(f"task title must be a string, got {type(title).__name__}")
    cleaned = title.strip()
    if not cleaned:
        raise ValueError("task title cannot be empty")
    if len(cleaned) > MAX_TASK_TITLE_CHARS:
        raise ValueError(
            f"task title exceeds maximum length of {MAX_TASK_TITLE_CHARS} characters "
            f"(got {len(cleaned)})"
        )
    return cleaned


def validate_task_description(description: Any) -> str:
    if description is None:
        return ""
    if not isinstance(description, str):
        raise ValueError(f"task description must be a string, got {type(description).__name__}")
    if len(description) > MAX_TASK_DESCRIPTION_CHARS:
        raise ValueError(
            f"task description exceeds maximum length of {MAX_TASK_DESCRIPTION_CHARS} characters "
            f"(got {len(description)})"
        )
    return description


def validate_task_status(status: Any) -> TaskStatus:
    if isinstance(status, TaskStatus):
        return status
    if isinstance(status, str):
        try:
            return TaskStatus(status.lower())
        except ValueError:
            pass
    allowed = ", ".join(s.value for s in TaskStatus)
    raise ValueError(f"invalid task status: {status!r}; expected one of: {allowed}")


def validate_task_priority(priority: Any) -> TaskPriority:
    if isinstance(priority, TaskPriority):
        return priority
    if isinstance(priority, str):
        try:
            return TaskPriority(priority.lower())
        except ValueError:
            pass
    allowed = ", ".join(p.value for p in TaskPriority)
    raise ValueError(f"invalid task priority: {priority!r}; expected one of: {allowed}")


def validate_task_labels(labels: Any) -> list[str]:
    if labels is None:
        return []
    if not isinstance(labels, (list, tuple, set)):
        raise ValueError(f"task labels must be a list of strings, got {type(labels).__name__}")
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in labels:
        if not isinstance(item, str):
            raise ValueError(f"task label must be a string, got {type(item).__name__}")
        tag = item.strip().lower()
        if not tag or tag in seen:
            continue
        if len(tag) > MAX_LABEL_CHARS:
            raise ValueError(f"task label exceeds {MAX_LABEL_CHARS} characters: {tag!r}")
        cleaned.append(tag)
        seen.add(tag)
        if len(cleaned) > MAX_LABELS_COUNT:
            raise ValueError(f"exceeded maximum allowed labels count of {MAX_LABELS_COUNT}")
    return cleaned


@dataclass
class ProjectTask:
    """项目级待办事项实体。"""

    id: str
    title: str
    description: str = ""
    status: TaskStatus = TaskStatus.TODO
    priority: TaskPriority = TaskPriority.MEDIUM
    labels: list[str] = field(default_factory=list)
    assigned_thread_id: str | None = None
    created_at_ms: int = 0
    updated_at_ms: int = 0
    completed_at_ms: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "status": self.status.value,
            "priority": self.priority.value,
            "labels": list(self.labels),
            "assigned_thread_id": self.assigned_thread_id,
            "created_at_ms": self.created_at_ms,
            "updated_at_ms": self.updated_at_ms,
            "completed_at_ms": self.completed_at_ms,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProjectTask:
        completed_at = data.get("completed_at_ms")
        return cls(
            id=str(data["id"]),
            title=str(data["title"]),
            description=str(data.get("description") or ""),
            status=TaskStatus(str(data["status"])),
            priority=TaskPriority(str(data.get("priority") or TaskPriority.MEDIUM.value)),
            labels=[str(x) for x in data.get("labels") or []],
            assigned_thread_id=(
                str(data["assigned_thread_id"])
                if data.get("assigned_thread_id")
                else None
            ),
            created_at_ms=int(data.get("created_at_ms") or 0),
            updated_at_ms=int(data.get("updated_at_ms") or 0),
            completed_at_ms=int(completed_at) if completed_at is not None else None,
        )
