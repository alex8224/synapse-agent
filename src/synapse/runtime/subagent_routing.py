"""Heuristic routing between disabled and DAG subagent modes."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class SubagentRoutingDecision:
    use_parallel: bool
    score: int
    reason: str


_CONTINUE_RE = re.compile(
    r"^\s*(继续|继续吧|可以|开始|go on|continue|yes|ok|好的|嗯|嗯嗯)\s*[。.!！]*\s*$",
    re.IGNORECASE,
)

_EXPLICIT_PARALLEL_RE = re.compile(
    r"(多个\s*子\s*agent|多\s*个\s*agent|多\s*agent|多智能体|并行\s*子\s*agent|"
    r"并行分析|parallel[_\s-]*subagents?|dag)",
    re.IGNORECASE,
)

_BROAD_TASK_RE = re.compile(
    r"(全面分析|深度分析|全量分析|系统性分析|整体分析|分析项目|梳理项目|整个项目|"
    r"全项目|全仓库|代码库|架构分析|架构设计|架构审计|安全审计|性能审计|"
    r"生成.*文档|教学文档|迁移|重构|改造|升级|端到端|全链路)",
    re.IGNORECASE,
)

_DEBUG_COMPLEX_RE = re.compile(
    r"(定位.*修复|分析.*修复|冲突.*解决|解决.*冲突|回归|失效|疑难|复杂|"
    r"traceback|cannot schedule|deadlock|race|竞态)",
    re.IGNORECASE,
)

_AREA_WORDS = (
    "架构",
    "核心",
    "ui",
    "界面",
    "配置",
    "文档",
    "测试",
    "ci",
    "工作流",
    "模型",
    "工具",
    "中间件",
    "会话",
    "数据库",
    "性能",
    "安全",
    "发布",
)


def decide_subagent_routing(
    task: str,
    *,
    current_parallel: bool = False,
) -> SubagentRoutingDecision:
    """Decide whether one user task merits DAG parallel subagents.

    The classifier is intentionally conservative: no subagent is cheaper and
    sufficient for short, single-focus turns. Parallel DAG is selected only when
    the user explicitly asks for parallel/multi-agent work or the request has
    broad, multi-area, project-level signals.
    """
    text = " ".join(str(task or "").split())
    if not text:
        return SubagentRoutingDecision(False, 0, "empty task")
    if _CONTINUE_RE.match(text):
        return SubagentRoutingDecision(
            bool(current_parallel),
            0,
            "continuation keeps current mode",
        )

    score = 0
    reasons: list[str] = []

    if _EXPLICIT_PARALLEL_RE.search(text):
        score += 4
        reasons.append("explicit parallel/multi-agent intent")
    if _BROAD_TASK_RE.search(text):
        score += 3
        reasons.append("broad project-level task")
    if _DEBUG_COMPLEX_RE.search(text) and len(text) >= 40:
        score += 2
        reasons.append("complex debug/change signal")

    area_hits = {
        word for word in _AREA_WORDS if word.casefold() in text.casefold()
    }
    if len(area_hits) >= 3:
        score += 2
        reasons.append("multiple technical areas")

    if len(text) >= 260:
        score += 2
        reasons.append("long multi-part request")
    elif len(text) >= 140:
        score += 1
        reasons.append("medium-length request")

    numbered_items = len(re.findall(r"(^|\s)(\d+[.、)]|[-*]\s+)", text))
    separators = sum(text.count(mark) for mark in ("；", ";", "\n", "、"))
    if numbered_items >= 3 or separators >= 4:
        score += 2
        reasons.append("enumerated/multi-item request")
    elif numbered_items >= 2 or separators >= 2:
        score += 1
        reasons.append("several requested items")

    use_parallel = score >= 4
    reason = ", ".join(reasons) if reasons else "single-focus task"
    return SubagentRoutingDecision(use_parallel, score, reason)
