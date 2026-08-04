"""长程任务目标（goal）领域模型。

移植自 Codex 的 thread goal 设计：每个 thread 至多一个持久化 goal，
状态机由系统（budget/usage）与 Agent 工具（complete/blocked）共同推进。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

MAX_GOAL_OBJECTIVE_CHARS = 10_000


class ThreadGoalStatus(StrEnum):
    """Goal 生命周期状态（与 Codex thread_goals 对齐）。"""

    ACTIVE = "active"
    PAUSED = "paused"
    BLOCKED = "blocked"
    USAGE_LIMITED = "usage_limited"
    BUDGET_LIMITED = "budget_limited"
    COMPLETE = "complete"

    def is_active(self) -> bool:
        return self is ThreadGoalStatus.ACTIVE

    def is_terminal(self) -> bool:
        return self in {
            ThreadGoalStatus.COMPLETE,
            ThreadGoalStatus.BLOCKED,
            ThreadGoalStatus.USAGE_LIMITED,
        }

    def is_replaceable(self) -> bool:
        """已完成 / 阻塞 / 用量受限的目标可被新目标替换。"""
        return self.is_terminal()

    def label(self) -> str:
        return {
            ThreadGoalStatus.ACTIVE: "active",
            ThreadGoalStatus.PAUSED: "paused",
            ThreadGoalStatus.BLOCKED: "stalled",
            ThreadGoalStatus.USAGE_LIMITED: "usage limited",
            ThreadGoalStatus.BUDGET_LIMITED: "limited by budget",
            ThreadGoalStatus.COMPLETE: "complete",
        }[self]


def new_goal_id() -> str:
    return uuid.uuid4().hex


@dataclass
class ThreadGoal:
    """一个 thread 的持久化目标及用量统计。"""

    thread_id: str
    goal_id: str
    objective: str
    status: ThreadGoalStatus
    token_budget: int | None = None
    tokens_used: int = 0
    time_used_seconds: int = 0
    created_at_ms: int = 0
    updated_at_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "goal_id": self.goal_id,
            "objective": self.objective,
            "status": self.status.value,
            "token_budget": self.token_budget,
            "tokens_used": self.tokens_used,
            "time_used_seconds": self.time_used_seconds,
            "created_at_ms": self.created_at_ms,
            "updated_at_ms": self.updated_at_ms,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ThreadGoal:
        return cls(
            thread_id=str(data["thread_id"]),
            goal_id=str(data["goal_id"]),
            objective=str(data["objective"]),
            status=ThreadGoalStatus(str(data["status"])),
            token_budget=_opt_int(data.get("token_budget")),
            tokens_used=int(data.get("tokens_used") or 0),
            time_used_seconds=int(data.get("time_used_seconds") or 0),
            created_at_ms=int(data.get("created_at_ms") or 0),
            updated_at_ms=int(data.get("updated_at_ms") or 0),
        )

    @property
    def remaining_tokens(self) -> int | None:
        if self.token_budget is None:
            return None
        return max(0, self.token_budget - self.tokens_used)


def _opt_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def validate_goal_objective(objective: str) -> str | None:
    """校验 objective，返回错误信息（None 表示合法）。"""
    text = (objective or "").strip()
    if not text:
        return "goal objective must not be empty"
    if len(text) > MAX_GOAL_OBJECTIVE_CHARS:
        return f"goal objective too long: {len(text)} > {MAX_GOAL_OBJECTIVE_CHARS} chars"
    return None


def validate_goal_budget(token_budget: int | None) -> str | None:
    if token_budget is None:
        return None
    if not isinstance(token_budget, int) or token_budget <= 0:
        return "token_budget must be a positive integer"
    return None


def goal_token_delta(input_tokens: int, output_tokens: int, cache_read_tokens: int = 0) -> int:
    """按 Codex 口径折算 goal 计费 token：input - cached_input + output。"""
    billable_input = max(0, max(0, input_tokens) - max(0, cache_read_tokens))
    return billable_input + max(0, output_tokens)
