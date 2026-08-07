"""``/goal`` slash 命令：长程目标查看与操作。

对应 Codex TUI 的 ``/goal``（GOAL_USAGE）与 goal 摘要渲染。
命令形态：
- ``/goal`` 或 ``/goal show``：显示当前目标摘要
- ``/goal <objective>``：设置新目标（已有未完成目标时给出提示）
- ``/goal edit <objective>``：编辑当前目标文本
- ``/goal clear``：清除目标
- ``/goal pause`` / ``/goal resume``：暂停 / 恢复目标
- 别名 ``gooooal``（g + o* + al）与 Codex 一致
"""

from __future__ import annotations

from typing import Any

from synapse.commands.result import SlashResult
from synapse.goals.model import ThreadGoal, ThreadGoalStatus, validate_goal_objective
from synapse.goals.runtime import get_goal_service

GOAL_USAGE = "Usage: /goal [<objective>|show|clear|edit <objective>|pause|resume]"

_GOAL_CONTROL = {
    "clear",
    "pause",
    "resume",
}


def format_goal_elapsed(seconds: int) -> str:
    """紧凑时间格式：0s / 59s / 1m / 1h 30m / 1d 2h 3m（同 Codex）。"""
    seconds = max(0, int(seconds or 0))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours, rem_min = divmod(minutes, 60)
    if hours >= 24:
        days, rem_hours = divmod(hours, 24)
        return f"{days}d {rem_hours}h {rem_min}m"
    if rem_min == 0:
        return f"{hours}h"
    return f"{hours}h {rem_min}m"


def goal_summary_lines(goal: ThreadGoal) -> list[str]:
    """goal 摘要纯文本行（CLI / TUI 通用）。"""
    from synapse.ui.formatters import format_token_count

    lines = [
        f"Status: {goal.status.label()}",
        f"Objective: {goal.objective}",
        f"Time used: {format_goal_elapsed(goal.time_used_seconds)}",
        f"Tokens used: {format_token_count(goal.tokens_used)}",
    ]
    if goal.token_budget is not None:
        lines.append(f"Token budget: {format_token_count(goal.token_budget)}")
    command_hint = {
        ThreadGoalStatus.ACTIVE: "Commands: /goal edit, /goal pause, /goal clear",
        ThreadGoalStatus.PAUSED: "Commands: /goal edit, /goal resume, /goal clear",
        ThreadGoalStatus.BLOCKED: "Commands: /goal edit, /goal resume, /goal clear",
        ThreadGoalStatus.USAGE_LIMITED: "Commands: /goal edit, /goal resume, /goal clear",
        ThreadGoalStatus.BUDGET_LIMITED: "Commands: /goal edit, /goal clear",
        ThreadGoalStatus.COMPLETE: "Commands: /goal edit, /goal clear",
    }[goal.status]
    lines.append(command_hint)
    return lines


def handle_goal(args: list[str], *, thread_id: str | None, settings: Any = None) -> SlashResult:
    """``/goal`` 命令入口。settings 仅用于读取 sessions 路径以定位存储。"""
    del settings  # 存储路径经进程级 GoalService 解析
    service = get_goal_service()
    if service is None:
        return SlashResult(
            handled=True,
            lines=["goal service unavailable (agent not built yet)"],
            error=True,
        )

    if not thread_id:
        return SlashResult(
            handled=True,
            lines=[GOAL_USAGE, "The session must start before you can set a goal."],
        )

    text = " ".join(args).strip() if args else ""
    cmd = args[0].casefold() if args else ""

    if cmd in {"show", "status"} or not text:
        goal = service.get(thread_id)
        if goal is None:
            return SlashResult(
                handled=True,
                lines=[GOAL_USAGE, "No goal is currently set."],
            )
        return SlashResult(handled=True, lines=goal_summary_lines(goal))

    if cmd in _GOAL_CONTROL or cmd == "edit":
        if cmd == "clear":
            goal, error = service.clear_goal(thread_id)
        elif cmd == "pause":
            goal, error = service.pause_goal(thread_id)
        elif cmd == "resume":
            goal, error = service.resume_goal(thread_id)
        else:  # edit
            new_objective = " ".join(args[1:]).strip()
            invalid = validate_goal_objective(new_objective)
            if invalid:
                return SlashResult(handled=True, lines=[f"goal edit failed: {invalid}"], error=True)
            goal, error = service.edit_goal(thread_id, new_objective)
        if error:
            return SlashResult(handled=True, lines=[error], error=True)
        assert goal is not None
        action = {"clear": "cleared", "pause": "paused", "resume": "resumed", "edit": "edited"}[cmd]
        return SlashResult(
            handled=True,
            lines=[f"goal {action}.", *goal_summary_lines(goal)],
            notice=f"goal {action}",
            cancel_active_turn=cmd == "pause",
        )

    # 设置新目标
    objective = text
    invalid = validate_goal_objective(objective)
    if invalid:
        return SlashResult(handled=True, lines=[f"failed to set goal: {invalid}"], error=True)
    goal, error = service.set_goal(thread_id, objective, replace=False)
    if error:
        return SlashResult(handled=True, lines=[error, GOAL_USAGE], error=True)
    assert goal is not None
    return SlashResult(
        handled=True,
        lines=["goal set.", *goal_summary_lines(goal)],
        notice="goal set",
    )


def is_goal_command(cmd: str) -> bool:
    """识别 ``/goal`` 及其 ``gooooal`` 别名。"""
    name = cmd.casefold()
    if name == "/goal":
        return True
    if name.startswith("/g") and name.endswith("al"):
        middle = name[2:-2]
        return bool(middle and set(middle) == {"o"})
    return False