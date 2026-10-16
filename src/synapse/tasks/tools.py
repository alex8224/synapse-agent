"""Agent 可调用的项目级待办事项（Project Tasks）工具集。

提供 list_project_tasks / get_project_task / create_project_task /
update_project_task / delete_project_task，供 Agent 查阅项目长期规划与协同维护。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from synapse.tasks.model import (
    ProjectTask,
    validate_task_description,
    validate_task_labels,
    validate_task_priority,
    validate_task_status,
    validate_task_title,
)
from synapse.tasks.service import ProjectTaskService, get_task_service


def _extract_thread_id(runtime: Any) -> str | None:
    config = dict(getattr(runtime, "config", None) or {})
    configurable = dict(config.get("configurable") or {})
    tid = configurable.get("thread_id")
    return str(tid) if tid else None


def _format_task_summary(task: ProjectTask) -> str:
    assigned = f" (assigned: {task.assigned_thread_id})" if task.assigned_thread_id else ""
    labels = f" [{', '.join(task.labels)}]" if task.labels else ""
    prefix = f"- [{task.id}] ({task.status.value}/{task.priority.value}){labels}"
    return f"{prefix} {task.title}{assigned}"


def _format_task_detail(task: ProjectTask) -> str:
    lines = [
        f"ID: {task.id}",
        f"Title: {task.title}",
        f"Status: {task.status.value} ({task.status.label()})",
        f"Priority: {task.priority.value} ({task.priority.label()})",
        f"Labels: {', '.join(task.labels) if task.labels else 'none'}",
        f"Assigned Thread: {task.assigned_thread_id or 'none'}",
        f"Created At: {task.created_at_ms}",
        f"Updated At: {task.updated_at_ms}",
        f"Completed At: {task.completed_at_ms or 'none'}",
        "Description:",
        task.description if task.description else "(no description)",
    ]
    return "\n".join(lines)


def build_task_tools(
    service: ProjectTaskService | None = None,
    tasks_path: Path | str | None = None,
) -> list[Any]:
    """构建项目级任务工具集。"""

    def _resolve_service() -> ProjectTaskService | None:
        if service is not None:
            return service
        if tasks_path is not None:
            return get_task_service(tasks_path)
        return None

    @tool
    def list_project_tasks(
        status: Literal["todo", "in_progress", "blocked", "done", "all"] = "all",
        label: str | None = None,
        limit: int = 50,
    ) -> str:
        """列出当前项目的长期待办事项（Backlog / Tasks）。

        可按状态（todo/in_progress/blocked/done/all）或标签筛选。
        """
        srv = _resolve_service()
        if srv is None:
            return "Project task service is not available."

        filter_status = None
        if status and status != "all":
            try:
                filter_status = validate_task_status(status)
            except ValueError as e:
                return f"Error: {e}"

        tasks = srv.list_tasks(status=filter_status, label=label, limit=limit)
        if not tasks:
            status_desc = f" with status '{status}'" if status != "all" else ""
            label_desc = f" and label '{label}'" if label else ""
            return f"No project tasks found{status_desc}{label_desc}."

        header = f"Found {len(tasks)} project task(s):\n"
        body = "\n".join(_format_task_summary(t) for t in tasks)
        return header + body

    @tool
    def get_project_task(task_id: str) -> str:
        """获取单个项目待办事项的详细信息，包括描述与关联会话。"""
        srv = _resolve_service()
        if srv is None:
            return "Project task service is not available."
        clean_id = (task_id or "").strip()
        if not clean_id:
            return "Error: task_id cannot be empty."
        task = srv.get_task(clean_id)
        if task is None:
            return f"Project task not found: {clean_id}"
        return _format_task_detail(task)

    @tool
    def create_project_task(
        runtime: ToolRuntime,
        title: str,
        description: str = "",
        priority: Literal["low", "medium", "high", "critical"] = "medium",
        labels: list[str] | None = None,
        assign_to_current_session: bool = False,
    ) -> str:
        """为当前项目创建一个新的长期待办事项（Project Task）。

        当讨论中确认了未来要做的改进、Bug修复或新功能时使用，将其持久化记录在项目中。
        """
        srv = _resolve_service()
        if srv is None:
            return "Project task service is not available."
        try:
            clean_title = validate_task_title(title)
            clean_desc = validate_task_description(description)
            valid_prio = validate_task_priority(priority)
            clean_labels = validate_task_labels(labels)
        except ValueError as err:
            return f"Validation error: {err}"

        assigned_thread: str | None = None
        if assign_to_current_session and runtime is not None:
            assigned_thread = _extract_thread_id(runtime)

        task = srv.create_task(
            title=clean_title,
            description=clean_desc,
            priority=valid_prio,
            labels=clean_labels,
            assigned_thread_id=assigned_thread,
        )
        return f"Project task created successfully:\n{_format_task_detail(task)}"

    @tool
    def update_project_task(
        runtime: ToolRuntime,
        task_id: str,
        status: Literal["todo", "in_progress", "blocked", "done"] | None = None,
        priority: Literal["low", "medium", "high", "critical"] | None = None,
        title: str | None = None,
        description: str | None = None,
        labels: list[str] | None = None,
        assign_to_current_session: bool = False,
        clear_assigned_thread: bool = False,
    ) -> str:
        """更新已有项目待办事项的状态、优先级、描述或关联会话。

        例如任务开始执行时置为 in_progress，完成时置为 done。
        """
        srv = _resolve_service()
        if srv is None:
            return "Project task service is not available."
        clean_id = (task_id or "").strip()
        if not clean_id:
            return "Error: task_id cannot be empty."

        assigned_thread: str | None = None
        if assign_to_current_session and runtime is not None:
            assigned_thread = _extract_thread_id(runtime)

        try:
            updated = srv.update_task(
                clean_id,
                title=title,
                description=description,
                status=status,
                priority=priority,
                labels=labels,
                assigned_thread_id=assigned_thread,
                clear_assigned_thread=clear_assigned_thread,
            )
        except ValueError as err:
            return f"Validation error: {err}"

        if updated is None:
            return f"Project task not found: {clean_id}"
        return f"Project task updated successfully:\n{_format_task_detail(updated)}"

    @tool
    def delete_project_task(task_id: str) -> str:
        """删除某个不再需要的项目待办事项。"""
        srv = _resolve_service()
        if srv is None:
            return "Project task service is not available."
        clean_id = (task_id or "").strip()
        if not clean_id:
            return "Error: task_id cannot be empty."
        success = srv.delete_task(clean_id)
        if not success:
            return f"Project task not found or already deleted: {clean_id}"
        return f"Project task {clean_id} deleted."

    return [
        list_project_tasks,
        get_project_task,
        create_project_task,
        update_project_task,
        delete_project_task,
    ]
