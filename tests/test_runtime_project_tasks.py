"""测试运行时服务层的 project tasks 契约、本地实现与 ACL 鉴权。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from synapse.runtime.service import (
    PROJECT_LIST,
    PROJECT_TASKS,
    AclAuthorizer,
    AclGrant,
    LocalAgentRuntimeService,
    PermissionDeniedError,
    Principal,
    bind_access,
)
from synapse.runtime.service.project_tasks import (
    CreateProjectTaskCommand,
    DeleteProjectTaskCommand,
    GetProjectTaskQuery,
    ListProjectTasksQuery,
    UpdateProjectTaskCommand,
)
from synapse.runtime.sessions import RuntimeManager
from synapse.runtime.transport.protocol import decode_params


def _manager(project_id: str, tasks_path: Path) -> RuntimeManager:
    settings = SimpleNamespace(
        resolved_sessions_path=lambda: tasks_path.parent / "sessions.sqlite",
        resolved_tasks_path=lambda: tasks_path,
        max_concurrency=1,
        model="test",
    )
    return RuntimeManager(
        settings=settings,
        agent_factory=lambda thread_id, shared: SimpleNamespace(thread_id=thread_id),
        project_id=project_id,
        max_concurrent_sessions=1,
    )


def test_local_service_project_tasks_lifecycle(tmp_path: Path) -> None:
    tasks_path = tmp_path / "tasks.sqlite"
    manager = _manager("proj-1", tasks_path)

    def _resolve(pid: str) -> RuntimeManager | None:
        return manager if pid == "proj-1" else None
    service = LocalAgentRuntimeService(_resolve)

    # 1. 初始列表为空
    page = asyncio.run(
        service.list_project_tasks(ListProjectTasksQuery(project_id="proj-1"))
    )
    assert page.project_id == "proj-1"
    assert len(page.tasks) == 0
    assert page.total_count == 0

    # 2. 创建任务
    create_res = asyncio.run(
        service.create_project_task(
            CreateProjectTaskCommand(
                project_id="proj-1",
                title="Implement Jira Board",
                description="Web console task board UI",
                priority="high",
                labels=("ui", "feature"),
            )
        )
    )
    assert create_res.status == "ok"
    assert create_res.task is not None
    assert create_res.task.title == "Implement Jira Board"
    assert create_res.task.priority == "high"
    assert "ui" in create_res.task.labels
    task_id = create_res.task.id

    # 3. 获取任务详情
    task_view = asyncio.run(
        service.get_project_task(GetProjectTaskQuery(project_id="proj-1", task_id=task_id))
    )
    assert task_view is not None
    assert task_view.id == task_id
    assert task_view.status == "todo"

    # 4. 更新任务状态
    up_res = asyncio.run(
        service.update_project_task(
            UpdateProjectTaskCommand(
                project_id="proj-1",
                task_id=task_id,
                status="in_progress",
                assigned_thread_id="thread-xyz",
            )
        )
    )
    assert up_res.status == "ok"
    assert up_res.task is not None
    assert up_res.task.status == "in_progress"
    assert up_res.task.assigned_thread_id == "thread-xyz"

    # 5. 过滤查询
    filtered = asyncio.run(
        service.list_project_tasks(
            ListProjectTasksQuery(project_id="proj-1", status="in_progress")
        )
    )
    assert len(filtered.tasks) == 1
    assert filtered.tasks[0].id == task_id

    # 6. 删除任务
    del_res = asyncio.run(
        service.delete_project_task(
            DeleteProjectTaskCommand(project_id="proj-1", task_id=task_id)
        )
    )
    assert del_res.deleted is True

    # 验证已删除
    task_after = asyncio.run(
        service.get_project_task(GetProjectTaskQuery(project_id="proj-1", task_id=task_id))
    )
    assert task_after is None


def test_project_tasks_acl_authorization(tmp_path: Path) -> None:
    tasks_path = tmp_path / "tasks.sqlite"
    manager = _manager("proj-1", tasks_path)

    def _resolve(pid: str) -> RuntimeManager | None:
        return manager if pid == "proj-1" else None
    delegate = LocalAgentRuntimeService(_resolve)

    # 1. 只有 project.tasks 权限的主体
    grant_ok = AclGrant(
        subject="user-ok",
        project_id="proj-1",
        capabilities=frozenset({PROJECT_TASKS}),
    )
    authorizer_ok = AclAuthorizer((grant_ok,))
    service_ok = bind_access(delegate, Principal("user-ok"), authorizer_ok)

    # 有权限时可以调用
    page = asyncio.run(
        service_ok.list_project_tasks(ListProjectTasksQuery(project_id="proj-1"))
    )
    assert page.project_id == "proj-1"

    # 2. 没有任何权限的主体
    grant_denied = AclGrant(
        subject="user-denied",
        project_id="proj-1",
        capabilities=frozenset({PROJECT_LIST}),
    )
    authorizer_denied = AclAuthorizer((grant_denied,))
    service_denied = bind_access(delegate, Principal("user-denied"), authorizer_denied)

    # 无权限时被拒绝
    with pytest.raises(PermissionDeniedError):
        asyncio.run(
            service_denied.list_project_tasks(ListProjectTasksQuery(project_id="proj-1"))
        )


def test_project_tasks_protocol_decode() -> None:
    # 1. create with assigned thread and empty description
    cmd = decode_params(
        "runtime.project.task.create",
        {
            "project_id": "proj-1",
            "title": "测试中文待办标题",
            "description": "",
            "priority": "high",
            "labels": ["bug", "frontend"],
            "assigned_thread_id": "thread-123",
        },
    )
    assert isinstance(cmd, CreateProjectTaskCommand)
    assert cmd.project_id == "proj-1"
    assert cmd.title == "测试中文待办标题"
    assert cmd.description == ""
    assert cmd.priority == "high"
    assert cmd.labels == ("bug", "frontend")
    assert cmd.assigned_thread_id == "thread-123"

    # 2. create with default description and no assigned thread
    cmd2 = decode_params(
        "runtime.project.task.create",
        {
            "project_id": "proj-1",
            "title": "另一个待办",
        },
    )
    assert isinstance(cmd2, CreateProjectTaskCommand)
    assert cmd2.assigned_thread_id is None
    assert cmd2.description == ""

    # 3. update with clear assigned thread
    up_cmd = decode_params(
        "runtime.project.task.update",
        {
            "project_id": "proj-1",
            "task_id": "task-abc",
            "title": "更新后的标题",
            "status": "in_progress",
            "clear_assigned_thread": True,
        },
    )
    assert isinstance(up_cmd, UpdateProjectTaskCommand)
    assert up_cmd.title == "更新后的标题"
    assert up_cmd.clear_assigned_thread is True


def test_runtime_edit_and_status_roundtrip_persist(tmp_path: Path) -> None:
    from synapse.tasks.service import reset_task_service

    path = tmp_path / "tasks.sqlite"
    manager = _manager("proj-1", path)
    service = LocalAgentRuntimeService(lambda pid: manager if pid == "proj-1" else None)
    try:
        created = asyncio.run(service.create_project_task(CreateProjectTaskCommand(
            project_id="proj-1", title="Original", description="Description",
            priority="low", labels=("old",), assigned_thread_id="session-1",
        )))
        assert created.task is not None
        task_id = created.task.id
        edited = asyncio.run(service.update_project_task(UpdateProjectTaskCommand(
            project_id="proj-1", task_id=task_id, title="Edited", description="",
            priority="critical", labels=(),
        )))
        assert edited.task is not None
        assert edited.task.title == "Edited"
        assert edited.task.description == ""
        assert edited.task.labels == ()
        assert edited.task.priority == "critical"
        assert edited.task.assigned_thread_id == "session-1"

        for status in ("in_progress", "blocked", "done", "todo", "done", "blocked"):
            result = asyncio.run(service.update_project_task(UpdateProjectTaskCommand(
                project_id="proj-1", task_id=task_id, status=status,
            )))
            assert result.status == "ok"
            assert result.task is not None
            assert result.task.status == status
            assert (result.task.completed_at_ms is not None) == (status == "done")
            persisted = asyncio.run(service.get_project_task(GetProjectTaskQuery(
                project_id="proj-1", task_id=task_id,
            )))
            assert persisted == result.task
            assert persisted.title == "Edited"
            assert persisted.assigned_thread_id == "session-1"

        missing = asyncio.run(service.update_project_task(UpdateProjectTaskCommand(
            project_id="proj-1", task_id="nonexistent", title="Never saved",
        )))
        assert missing.status == "not_found"
        assert missing.task is None
    finally:
        reset_task_service(path)


@pytest.mark.parametrize("status", ["todo", "in_progress", "blocked", "done"])
def test_protocol_decodes_cleared_edit_fields_and_every_status(status: str) -> None:
    command = decode_params("runtime.project.task.update", {
        "project_id": "proj-1", "task_id": "task-1", "title": "Edited",
        "description": "", "priority": "critical", "labels": [], "status": status,
    })
    assert isinstance(command, UpdateProjectTaskCommand)
    assert command.title == "Edited"
    assert command.description == ""
    assert command.labels == ()
    assert command.priority == "critical"
    assert command.status == status
    assert command.assigned_thread_id is None
    assert command.clear_assigned_thread is False
