"""Bottombar component: long-running goal status indicator.

移植自 Codex ``goal_status.rs`` 的状态指示器：active 显示用量（有预算显示
token 用量，无预算显示时间），其余状态显示标签。
"""

from __future__ import annotations

from rich.text import Text

from synapse.commands.goal import format_goal_elapsed
from synapse.ui.bottombar.context import BottomBarContext
from synapse.ui.bottombar.core import BottomBarRegion, BottomBarRegistry
from synapse.ui.formatters import format_token_count

ID = "goal"
REGION = BottomBarRegion.LEFT
ORDER = 12
PRIORITY = 48  # drop before codex usage when narrow
MIN_WIDTH = 0

_STATUS_STYLE = {
    "active": "bold",
    "paused": "dim",
    "stalled": "yellow",
    "usage limited": "yellow",
    "limited by budget": "yellow",
    "complete": "green",
}


def goal_indicator_text(goal: object) -> str:
    """把 ThreadGoal 渲染为 bottombar 单行文本（与 goal_status.rs 对齐）。"""
    status_label = goal.status.label()  # type: ignore[attr-defined]
    parts = [f"goal·{status_label}"]
    if status_label == "active":
        if goal.token_budget is not None:  # type: ignore[attr-defined]
            parts.append(
                f"{format_token_count(goal.tokens_used)}/{format_token_count(goal.token_budget)}"  # type: ignore[attr-defined]
            )
        else:
            parts.append(format_goal_elapsed(goal.time_used_seconds))  # type: ignore[attr-defined]
    elif status_label == "complete":
        parts.append(format_token_count(goal.tokens_used))  # type: ignore[attr-defined]
    return " ".join(parts)


def install(registry: BottomBarRegistry, ctx: BottomBarContext) -> None:
    """Register the goal status label on the left (model/mcp area)."""

    def render() -> str | Text:
        value = ctx.goal()
        if not value:
            return ""
        if isinstance(value, Text):
            return value
        text = str(value).strip()
        if not text:
            return ""
        label = text.split("·", 1)[-1].split("/", 1)[0].strip() if "·" in text else ""
        style = _STATUS_STYLE.get(label, "")
        return Text(text, style=style) if style else text

    registry.register_fn(
        ID,
        render,
        region=REGION,
        order=ORDER,
        priority=PRIORITY,
        min_width=MIN_WIDTH,
    )
