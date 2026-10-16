"""测试项目任务 Agent 工具集。"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from synapse.tasks.model import TaskStatus
from synapse.tasks.service import get_task_service, reset_task_service
from synapse.tasks.tools import build_task_tools


@dataclass
class _DummyRuntime:
    config: dict[str, Any]


def test_task_tools_flow() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "tasks.sqlite"
        try:
            service = get_task_service(db_path)
            tools_list = build_task_tools(service)
            tools_by_name = {t.name: t for t in tools_list}

            assert "list_project_tasks" in tools_by_name
            assert "get_project_task" in tools_by_name
            assert "create_project_task" in tools_by_name
            assert "update_project_task" in tools_by_name
            assert "delete_project_task" in tools_by_name

            # 1. 列表为空
            res = tools_by_name["list_project_tasks"].invoke({})
            assert "No project tasks found" in res

            # 2. 创建任务
            runtime = _DummyRuntime(config={"configurable": {"thread_id": "thread-abc"}})
            res = tools_by_name["create_project_task"].func(
                runtime,  # type: ignore[arg-type]
                title="Refactor router",
                description="Clean up route definitions",
                priority="high",
                labels=["refactor", "core"],
                assign_to_current_session=True,
            )
            assert "Project task created successfully" in res
            assert "Refactor router" in res
            assert "thread-abc" in res

            # 获取刚创建的任务 ID
            tasks = service.list_tasks()
            assert len(tasks) == 1
            tid = tasks[0].id

            # 3. 获取任务详情
            detail = tools_by_name["get_project_task"].invoke({"task_id": tid})
            assert "Clean up route definitions" in detail
            assert "high" in detail

            # 4. 更新任务状态
            up_res = tools_by_name["update_project_task"].func(
                runtime,  # type: ignore[arg-type]
                task_id=tid,
                status="in_progress",
            )
            assert "In Progress" in up_res
            assert service.get_task(tid).status == TaskStatus.IN_PROGRESS

            # 5. 列表筛选
            list_res = tools_by_name["list_project_tasks"].invoke({"status": "in_progress"})
            assert tid in list_res
            assert "Refactor router" in list_res

            # 6. 删除任务
            del_res = tools_by_name["delete_project_task"].invoke({"task_id": tid})
            assert f"Project task {tid} deleted" in del_res
            assert service.get_task(tid) is None
        finally:
            reset_task_service(db_path)
