"""长程目标（goal）子系统 —— 移植自 Codex ``ext/goal``。

提供：
- 持久化：``GoalStore``（SQLite ``thread_goals`` 表）
- 运行时：``GoalService`` / ``GoalRuntime``（token/时间记账、预算耗尽、
  状态推进、自动续跑引导）
- 工具：``get_goal`` / ``create_goal`` / ``update_goal``
- 命令：``/goal``（含 ``gooooal`` 别名）
- TUI：bottombar 状态指示器 + 回合结束自动继续
"""

from typing import Any

from synapse.goals.model import (
    MAX_GOAL_OBJECTIVE_CHARS,
    ThreadGoal,
    ThreadGoalStatus,
    goal_token_delta,
    validate_goal_budget,
    validate_goal_objective,
)
from synapse.goals.runtime import (
    GoalListener,
    GoalRuntime,
    GoalService,
    get_goal_service,
    init_goal_service,
    reset_goal_service,
)
from synapse.goals.store import GoalStore, GoalStoreError


def __getattr__(name: str) -> Any:
    """Lazily resolve the two heavy builders.

    ``synapse.goals.middleware`` imports ``langchain.agents`` (~1.7s) and
    ``synapse.goals.tools`` imports ``langchain.tools`` (~1.7s); both are
    only needed when an agent is actually assembled.  Keeping them out of
    the package import keeps the TUI startup path (bottombar goal indicator
    → ``commands.goal`` → ``goals``) free of that cost.
    """
    if name == "build_goal_middleware":
        from synapse.goals.middleware import build_goal_middleware

        return build_goal_middleware
    if name == "build_goal_tools":
        from synapse.goals.tools import build_goal_tools

        return build_goal_tools
    raise AttributeError(name)

__all__ = [
    "GoalListener",
    "GoalRuntime",
    "GoalService",
    "GoalStore",
    "GoalStoreError",
    "MAX_GOAL_OBJECTIVE_CHARS",
    "ThreadGoal",
    "ThreadGoalStatus",
    "build_goal_middleware",
    "build_goal_tools",
    "get_goal_service",
    "goal_token_delta",
    "init_goal_service",
    "reset_goal_service",
    "validate_goal_budget",
    "validate_goal_objective",
]
