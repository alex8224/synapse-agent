"""Project-level task/backlog DTOs for the Agent Runtime Service.

This module belongs to the contract layer: frozen dataclasses and validation logic.
It imports no catalog, transport, settings, UI, or session-execution module,
so the wire decoder, the in-process service, and the web console can all depend
on it without introducing cycles.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

MAX_TASK_TITLE_CHARS: Final = 200
MAX_TASK_DESCRIPTION_CHARS: Final = 20_000
MAX_LABELS_COUNT: Final = 20
MAX_LABEL_CHARS: Final = 50
TASK_LIST_LIMIT_DEFAULT: Final = 50
TASK_LIST_LIMIT_MAX: Final = 200


def _validate_non_empty_str(val: object, name: str) -> str:
    if type(val) is not str:
        raise ValueError(f"{name} must be a string, got {type(val).__name__!r}")
    cleaned = val.strip()
    if not cleaned:
        raise ValueError(f"{name} must not be empty")
    return cleaned


def _validate_int(val: object, name: str, minimum: int, maximum: int) -> int:
    if type(val) is not int:
        raise ValueError(f"{name} must be an integer, got {type(val).__name__!r}")
    if not (minimum <= val <= maximum):
        raise ValueError(f"{name} must be between {minimum} and {maximum}, got {val}")
    return val


def _validate_labels(labels: object) -> tuple[str, ...]:
    if labels is None:
        return ()
    if not isinstance(labels, (list, tuple)):
        msg = f"labels must be a list or tuple of strings, got {type(labels).__name__!r}"
        raise ValueError(msg)
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in labels:
        if type(item) is not str:
            raise ValueError(f"label must be a string, got {type(item).__name__!r}")
        tag = item.strip().lower()
        if not tag or tag in seen:
            continue
        if len(tag) > MAX_LABEL_CHARS:
            raise ValueError(f"label exceeds {MAX_LABEL_CHARS} characters: {tag!r}")
        cleaned.append(tag)
        seen.add(tag)
        if len(cleaned) > MAX_LABELS_COUNT:
            raise ValueError(f"exceeded maximum allowed labels count of {MAX_LABELS_COUNT}")
    return tuple(cleaned)


@dataclass(frozen=True, slots=True)
class ProjectTaskView:
    """Read-only projection of a project task on the wire."""

    id: str
    title: str
    description: str = ""
    status: str = "todo"
    priority: str = "medium"
    labels: tuple[str, ...] = ()
    assigned_thread_id: str | None = None
    created_at_ms: int = 0
    updated_at_ms: int = 0
    completed_at_ms: int | None = None


@dataclass(frozen=True, slots=True)
class ListProjectTasksQuery:
    """Query to list tasks of a project."""

    project_id: str
    status: str | None = None
    label: str | None = None
    limit: int = TASK_LIST_LIMIT_DEFAULT
    offset: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "project_id", _validate_non_empty_str(self.project_id, "project_id")
        )
        object.__setattr__(
            self,
            "limit",
            _validate_int(self.limit, "limit", 1, TASK_LIST_LIMIT_MAX),
        )
        object.__setattr__(
            self,
            "offset",
            _validate_int(self.offset, "offset", 0, 100_000),
        )
        if self.status is not None:
            st = str(self.status).strip().lower()
            if st not in {"todo", "in_progress", "blocked", "done", "all"}:
                raise ValueError(f"invalid status filter: {self.status!r}")
            object.__setattr__(self, "status", None if st == "all" else st)
        if self.label is not None:
            object.__setattr__(self, "label", str(self.label).strip().lower() or None)


@dataclass(frozen=True, slots=True)
class ProjectTaskListPage:
    """Paginated list of tasks."""

    project_id: str
    tasks: tuple[ProjectTaskView, ...] = ()
    total_count: int = 0


@dataclass(frozen=True, slots=True)
class GetProjectTaskQuery:
    """Query a single project task."""

    project_id: str
    task_id: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "project_id", _validate_non_empty_str(self.project_id, "project_id")
        )
        object.__setattr__(self, "task_id", _validate_non_empty_str(self.task_id, "task_id"))


@dataclass(frozen=True, slots=True)
class CreateProjectTaskCommand:
    """Command to create a project task."""

    project_id: str
    title: str
    description: str = ""
    priority: str = "medium"
    labels: tuple[str, ...] = ()
    assigned_thread_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "project_id", _validate_non_empty_str(self.project_id, "project_id")
        )
        title = _validate_non_empty_str(self.title, "title")
        if len(title) > MAX_TASK_TITLE_CHARS:
            raise ValueError(f"title exceeds {MAX_TASK_TITLE_CHARS} characters")
        object.__setattr__(self, "title", title)
        desc = str(self.description or "")
        if len(desc) > MAX_TASK_DESCRIPTION_CHARS:
            raise ValueError(f"description exceeds {MAX_TASK_DESCRIPTION_CHARS} characters")
        object.__setattr__(self, "description", desc)
        prio = str(self.priority or "medium").strip().lower()
        if prio not in {"low", "medium", "high", "critical"}:
            raise ValueError(f"invalid priority: {self.priority!r}")
        object.__setattr__(self, "priority", prio)
        object.__setattr__(self, "labels", _validate_labels(self.labels))
        tid = str(self.assigned_thread_id).strip() if self.assigned_thread_id else None
        object.__setattr__(self, "assigned_thread_id", tid or None)


@dataclass(frozen=True, slots=True)
class UpdateProjectTaskCommand:
    """Command to update a project task."""

    project_id: str
    task_id: str
    title: str | None = None
    description: str | None = None
    status: str | None = None
    priority: str | None = None
    labels: tuple[str, ...] | None = None
    assigned_thread_id: str | None = None
    clear_assigned_thread: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "project_id", _validate_non_empty_str(self.project_id, "project_id")
        )
        object.__setattr__(self, "task_id", _validate_non_empty_str(self.task_id, "task_id"))
        if self.title is not None:
            title = _validate_non_empty_str(self.title, "title")
            if len(title) > MAX_TASK_TITLE_CHARS:
                raise ValueError(f"title exceeds {MAX_TASK_TITLE_CHARS} characters")
            object.__setattr__(self, "title", title)
        if self.description is not None:
            desc = str(self.description)
            if len(desc) > MAX_TASK_DESCRIPTION_CHARS:
                raise ValueError(f"description exceeds {MAX_TASK_DESCRIPTION_CHARS} characters")
            object.__setattr__(self, "description", desc)
        if self.status is not None:
            st = str(self.status).strip().lower()
            if st not in {"todo", "in_progress", "blocked", "done"}:
                raise ValueError(f"invalid status: {self.status!r}")
            object.__setattr__(self, "status", st)
        if self.priority is not None:
            prio = str(self.priority).strip().lower()
            if prio not in {"low", "medium", "high", "critical"}:
                raise ValueError(f"invalid priority: {self.priority!r}")
            object.__setattr__(self, "priority", prio)
        if self.labels is not None:
            object.__setattr__(self, "labels", _validate_labels(self.labels))
        if self.assigned_thread_id is not None:
            tid = str(self.assigned_thread_id).strip() or None
            object.__setattr__(self, "assigned_thread_id", tid)


@dataclass(frozen=True, slots=True)
class DeleteProjectTaskCommand:
    """Command to delete a project task."""

    project_id: str
    task_id: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "project_id", _validate_non_empty_str(self.project_id, "project_id")
        )
        object.__setattr__(self, "task_id", _validate_non_empty_str(self.task_id, "task_id"))


@dataclass(frozen=True, slots=True)
class DeleteProjectTaskResult:
    """Result of deleting a task."""

    project_id: str
    task_id: str
    deleted: bool


@dataclass(frozen=True, slots=True)
class ProjectTaskResult:
    """Mutation result for create/update."""

    project_id: str
    task: ProjectTaskView | None
    status: str = "ok"
