"""测试项目任务（Project Tasks）领域模型、持久化与服务。"""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from synapse.tasks.model import (
    MAX_LABELS_COUNT,
    MAX_TASK_TITLE_CHARS,
    ProjectTask,
    TaskPriority,
    TaskStatus,
    validate_task_description,
    validate_task_labels,
    validate_task_priority,
    validate_task_status,
    validate_task_title,
)
from synapse.tasks.service import get_task_service, reset_task_service
from synapse.tasks.store import TaskStore, read_task_readonly, read_tasks_readonly


def test_model_validations() -> None:
    # 标题校验
    assert validate_task_title("  Fix bug in login  ") == "Fix bug in login"
    with pytest.raises(ValueError, match="cannot be empty"):
        validate_task_title("   ")
    with pytest.raises(ValueError, match="exceeds maximum length"):
        validate_task_title("a" * (MAX_TASK_TITLE_CHARS + 1))

    # 描述校验
    assert validate_task_description(None) == ""
    assert validate_task_description("hello") == "hello"

    # 状态与优先级
    assert validate_task_status("in_progress") == TaskStatus.IN_PROGRESS
    with pytest.raises(ValueError, match="invalid task status"):
        validate_task_status("invalid_status")

    assert validate_task_priority("high") == TaskPriority.HIGH
    with pytest.raises(ValueError, match="invalid task priority"):
        validate_task_priority("urgent")

    # 标签校验
    assert validate_task_labels(["Bug", "UI", "bug"]) == ["bug", "ui"]
    assert validate_task_labels(None) == []
    with pytest.raises(ValueError, match="exceeded maximum allowed labels"):
        validate_task_labels([f"tag{i}" for i in range(MAX_LABELS_COUNT + 1)])


def test_store_crud() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "tasks.sqlite"
        store = TaskStore(db_path)

        # 插入任务
        task1 = store.insert(
            title="Design API",
            description="Detail the REST / RPC contracts",
            priority=TaskPriority.HIGH,
            status=TaskStatus.TODO,
            labels=["architecture", "v1"],
        )
        assert task1.id.startswith("task-")
        assert task1.title == "Design API"
        assert task1.status == TaskStatus.TODO
        assert task1.priority == TaskPriority.HIGH
        assert task1.labels == ["architecture", "v1"]
        assert task1.completed_at_ms is None

        # 查询单个
        fetched = store.get(task1.id)
        assert fetched is not None
        assert fetched.id == task1.id
        assert fetched.title == task1.title

        # 更新任务状态
        updated = store.update(
            task1.id,
            status=TaskStatus.DONE,
            assigned_thread_id="thread-123",
        )
        assert updated is not None
        assert updated.status == TaskStatus.DONE
        assert updated.assigned_thread_id == "thread-123"
        assert updated.completed_at_ms is not None

        # 插入第二条任务
        task2 = store.insert(
            title="Frontend UI",
            status=TaskStatus.IN_PROGRESS,
            labels=["ui"],
        )

        # 列表查询
        all_tasks = store.list()
        assert len(all_tasks) == 2

        # 过滤查询
        done_tasks = store.list(status=TaskStatus.DONE)
        assert len(done_tasks) == 1
        assert done_tasks[0].id == task1.id

        ui_tasks = store.list(label="ui")
        assert len(ui_tasks) == 1
        assert ui_tasks[0].id == task2.id

        assert store.count() == 2
        assert store.count(status=TaskStatus.DONE) == 1

        # 只读读取
        ro_task = read_task_readonly(db_path, task1.id)
        assert ro_task is not None
        assert ro_task.id == task1.id

        ro_list = read_tasks_readonly(db_path, status=TaskStatus.DONE)
        assert len(ro_list) == 1

        # 删除
        assert store.delete(task1.id) is True
        assert store.get(task1.id) is None
        assert store.count() == 1

        store.close()


def test_service_listener_and_cache() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "tasks.sqlite"
        try:
            service = get_task_service(db_path)
            events: list[tuple[str, str]] = []

            def listener(task: ProjectTask | None, action: str) -> None:
                if task:
                    events.append((task.title, action))

            service.add_listener(listener)

            # 创建
            t = service.create_task("Write docs")
            assert len(events) == 1
            assert events[0] == ("Write docs", "create")

            # 更新
            service.update_task(t.id, status=TaskStatus.IN_PROGRESS)
            assert len(events) == 2
            assert events[1] == ("Write docs", "update")

            # 删除
            service.delete_task(t.id)
            assert len(events) == 3
            assert events[2] == ("Write docs", "delete")

            # 单例缓存复用
            service2 = get_task_service(db_path)
            assert service2 is service
        finally:
            reset_task_service(db_path)


@pytest.mark.parametrize("source", list(TaskStatus))
@pytest.mark.parametrize("target", list(TaskStatus))
def test_all_status_transitions_persist_completion_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: TaskStatus, target: TaskStatus
) -> None:
    from synapse.tasks import store as store_mod

    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(store_mod, "time", SimpleNamespace(time=lambda: clock.now))
    path = tmp_path / "tasks.sqlite"
    store = TaskStore(path)
    try:
        task = store.insert(
            "Status lifecycle", status=source, labels=["lifecycle"],
            assigned_thread_id="assigned-session",
        )
        clock.now = 2000.0
        updated = store.update(task.id, status=target)
        assert updated is not None
        assert updated.status == target
        assert updated.updated_at_ms == 2_000_000
        assert updated.created_at_ms == 1_000_000
        expected_completion = (
            task.completed_at_ms if source == TaskStatus.DONE else 2_000_000
        ) if target == TaskStatus.DONE else None
        assert updated.completed_at_ms == expected_completion
        assert updated.assigned_thread_id == "assigned-session"
        assert updated.labels == ["lifecycle"]
    finally:
        store.close()

    persisted = read_task_readonly(path, task.id)
    assert persisted is not None
    assert persisted.status == target
    assert persisted.completed_at_ms == expected_completion


def test_edit_fields_can_be_cleared_without_changing_status_or_assignment(tmp_path: Path) -> None:
    path = tmp_path / "tasks.sqlite"
    store = TaskStore(path)
    try:
        task = store.insert(
            "Original", description="Long description", status=TaskStatus.DONE,
            priority=TaskPriority.LOW, labels=["original"], assigned_thread_id="session-1",
        )
        updated = store.update(
            task.id, title="  Edited  ", description="", priority="critical", labels=[],
        )
        assert updated is not None
        assert updated.title == "Edited"
        assert updated.description == ""
        assert updated.priority == TaskPriority.CRITICAL
        assert updated.labels == []
        assert updated.status == TaskStatus.DONE
        assert updated.assigned_thread_id == "session-1"
        assert updated.completed_at_ms == task.completed_at_ms
    finally:
        store.close()

    persisted = read_task_readonly(path, task.id)
    assert persisted == updated


def test_invalid_edit_leaves_every_saved_field_intact(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite")
    try:
        task = store.insert("Original", description="Keep me", labels=["keep"])
        for updates in (
            {"title": "   ", "status": "done"},
            {"title": "Changed", "status": "unknown"},
            {"description": "Changed", "priority": "unknown"},
            {"labels": ["x" * 51]},
        ):
            with pytest.raises(ValueError):
                store.update(task.id, **updates)
            assert store.get(task.id) == task
    finally:
        store.close()
