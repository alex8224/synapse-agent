"""LLM-driven task decomposition and step tracking.

Design:
    - ``TaskPlanner.plan(task)`` asks a (cheap) model to decide whether the
      task is complex enough to decompose, and if so returns an ordered list
      of sub-tasks.
    - If the task is simple (< 3 inferred steps), it returns a single-element
      plan (no extra LLM call in the hot path).
    - The planner is designed to be called *before* the main agent invocation
      so the UI can show the plan and the main agent can execute step-by-step.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

DECOMPOSE_SYSTEM_PROMPT = """\
You are a task-planning specialist. Your job is to analyse a user task and,
when appropriate, break it into an ordered list of smaller, independently
executable steps.

Rules:
1. If the task is trivial (1-2 simple actions), return an empty array [].
2. Each step MUST be a self-contained instruction that a coding agent can
   execute without additional context.
3. Steps MUST be ordered by dependencies – earlier steps produce artefacts
   consumed by later steps.
4. Do NOT include meta-commentary.  Return ONLY a JSON array of strings.
5. Output nothing else – no markdown fences, no explanation.

Examples:

Task: "列出当前目录的文件"
→ []

Task: "检查 test_login 失败原因并修复"
→ ["运行 test_login 查看失败详情", "根据失败原因修复源码", "重新运行 test_login 确认通过"]

Task: "重构 auth.py 把 JWT 换成 session，更新 tests/test_auth.py，更新 README"
→ ["阅读 auth.py 理解当前 JWT 实现", "重构 auth.py 将 JWT 替换为 session", "更新 tests/test_auth.py 适配新实现", "更新 README 文档"]
"""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TaskPlan:
    steps: list[str] = field(default_factory=list)
    is_complex: bool = False

    def __bool__(self) -> bool:
        return self.is_complex


# ---------------------------------------------------------------------------
# TaskPlanner
# ---------------------------------------------------------------------------

_JSON_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)


class TaskPlanner:
    """LLM-driven task decomposer.

    Usage::

        planner = TaskPlanner(lightweight_model)
        plan = planner.plan("重构 auth 模块并更新测试")
        if plan:
            for step in plan.steps:
                print(f"  → {step}")
    """

    def __init__(
        self,
        model: Any,
        *,
        min_steps_for_plan: int = 3,
        max_plan_steps: int = 8,
    ) -> None:
        """*model* can be a lightweight/cheap LangChain ChatModel – planning
        does not need the strongest reasoning model."""
        self._model = model
        self._min_steps = min_steps_for_plan
        self._max_steps = max_plan_steps

    # -- public API -----------------------------------------------------------

    async def plan(self, task: str) -> TaskPlan:
        """Analyse *task* and return a plan (or a single-step fallback)."""
        # Quick heuristic: short tasks are almost never complex.
        if len(task.split()) < 6:
            return TaskPlan(steps=[task], is_complex=False)

        try:
            steps = await self._llm_decompose(task)
        except Exception:
            # If the LLM call fails for any reason, fall back to single-step.
            return TaskPlan(steps=[task], is_complex=False)

        if not steps or len(steps) < self._min_steps:
            return TaskPlan(steps=[task], is_complex=False)

        steps = steps[: self._max_steps]  # enforce ceiling
        return TaskPlan(steps=steps, is_complex=True)

    # -- internals ------------------------------------------------------------

    async def _llm_decompose(self, task: str) -> list[str]:
        from langchain_core.messages import HumanMessage, SystemMessage

        messages = [
            SystemMessage(content=DECOMPOSE_SYSTEM_PROMPT),
            HumanMessage(content=f"Task: {task}"),
        ]
        response = await self._model.ainvoke(messages)
        content = response.content
        if isinstance(content, list):
            content = "".join(str(part) for part in content)
        return self._parse_steps(str(content))

    def _parse_steps(self, raw: str) -> list[str]:
        """Extract a JSON array of strings from possibly noisy LLM output."""
        match = _JSON_ARRAY_RE.search(raw)
        if not match:
            return []
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []
        if not isinstance(parsed, list):
            return []
        return [str(item).strip() for item in parsed if isinstance(item, str) and item.strip()]


__all__ = ["TaskPlan", "TaskPlanner"]
