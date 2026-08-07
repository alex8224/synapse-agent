"""Agent 可调用的 goal 工具：get_goal / create_goal / update_goal。

移植自 Codex ``ext/goal`` 的 Responses API 工具。工具通过 ``ToolRuntime``
获取当前 thread_id，再经进程级 :class:`GoalService` 读写持久化目标。
"""

from __future__ import annotations

from typing import Any, Literal

from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from synapse.goals.model import (
    ThreadGoalStatus,
    validate_goal_budget,
    validate_goal_objective,
)
from synapse.goals.runtime import get_goal_service


def _thread_id(runtime: Any) -> str | None:
    config = dict(getattr(runtime, "config", None) or {})
    configurable = dict(config.get("configurable") or {})
    tid = configurable.get("thread_id")
    return str(tid) if tid else None


def _format_goal(goal: Any, *, include_budget_report: bool = False) -> str:
    lines = [
        f"status: {goal.status.label()}",
        f"objective: {goal.objective}",
        f"tokens_used: {goal.tokens_used}",
        f"time_used_seconds: {goal.time_used_seconds}",
    ]
    if goal.token_budget is not None:
        lines.append(f"token_budget: {goal.token_budget}")
        if include_budget_report:
            lines.append(f"final_token_usage: {goal.tokens_used}")
    return "\n".join(lines)


def build_goal_tools(service: Any | None = None) -> list[Any]:
    """构建三个 goal 工具（注入 coding agent）。

    ``service`` 显式传入时使用项目实例（P6-05）；否则回退到进程级单例。
    """

    def _service() -> Any | None:
        return service if service is not None else get_goal_service()

    @tool
    def get_goal(runtime: ToolRuntime) -> str:
        """获取当前会话（thread）的长程目标，包括状态、预算、token 与时间用量。

        无目标时返回说明文本。用于检查是否存在正在执行的目标及其进度。
        """
        service = _service()
        if service is None:
            return "goal service unavailable"
        goal = service.get(_thread_id(runtime))
        if goal is None:
            return "no goal is currently set for this thread"
        return _format_goal(goal)

    @tool
    def create_goal(
        runtime: ToolRuntime,
        objective: str,
        token_budget: int | None = None,
    ) -> str:
        """仅当用户或系统/开发者指令明确要求时才创建目标；不要从普通任务推断目标。

        创建前会先结算当前回合已产生的用量。若本线程已有未完成目标则失败，
        需要先调用 update_goal 完成旧目标（或由用户清除）。

        Args:
            objective: 要开始推进的具体目标文本。
            token_budget: 可选的正整数 token 预算；仅在明确要求时设置。
        """
        service = _service()
        if service is None:
            return "goal service unavailable"
        text = (objective or "").strip()
        error = validate_goal_objective(text) or validate_goal_budget(token_budget)
        if error:
            return f"failed to create goal: {error}"
        thread_id = _thread_id(runtime)
        service.on_tool_finish(thread_id)
        goal, error = service.set_goal(thread_id, text, token_budget=token_budget)
        if error:
            return f"failed to create goal: {error}"
        assert goal is not None
        return f"goal created.\n{_format_goal(goal)}"

    @tool
    def update_goal(runtime: ToolRuntime, status: Literal["complete", "blocked"]) -> str:
        """更新现有目标：仅用于把目标标记为已完成或真正受阻。

        - complete：仅当目标确实达成、所有必需工作已完成时使用。
        - blocked：仅当同一阻塞条件已连续出现至少三个目标回合（含原始回合与
          自动续跑回合），且没有用户输入或外部状态变化就无法取得实质进展时使用。
          不要因为工作困难、缓慢、不确定、未完成或想澄清就用 blocked。

        完成带预算的目标后，向用户报告最终 token 用量。
        """
        service = _service()
        if service is None:
            return "goal service unavailable"
        thread_id = _thread_id(runtime)
        service.on_tool_finish(thread_id)
        new_status = (
            ThreadGoalStatus.COMPLETE if status == "complete" else ThreadGoalStatus.BLOCKED
        )
        goal, error = service.mark_status(thread_id, new_status)
        if error:
            return f"failed to update goal: {error}"
        assert goal is not None
        return _format_goal(goal, include_budget_report=new_status == ThreadGoalStatus.COMPLETE)

    return [get_goal, create_goal, update_goal]
