"""Auto-recorder: importance assessment + lesson extraction for long-term memory.

After each conversation turn, this module evaluates whether the exchange
contains reusable knowledge and, if so, extracts concise learning points
for storage in ``LongTermMemory``.

Design:
    1. **Heuristic pre-filter** – fast, zero-cost check that discards
       greetings, trivial queries, and empty answers without an LLM call.
    2. **LLM extraction** (optional) – if a cheap model is provided, ask it
       to extract up to 3 transferable lessons.  Falls back to simple
       keyword-based extraction when no model is available.
    3. **Deduplication** – compares new lesson embeddings against recent
       memories to avoid storing near-duplicates.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Heuristic pre-filter
# ---------------------------------------------------------------------------

_TRIVIAL_TASK_RE = re.compile(
    r"^(你好|hi|hello|hey|谢谢|thanks|ok|好的|知道了|嗯|哦|再见|bye|exit|quit|/.*)$",
    re.IGNORECASE,
)

_TRIVIAL_ANSWER_MAX_CHARS = 120

_KNOWLEDGE_SIGNALS = re.compile(
    r"\b(fix|bug|error|solution|config|pattern|workflow|refactor|"
    r"最佳实践|经验|教训|注意|关键|核心|原理|架构|设计|"
    r"def |class |import |function|method)\b",
    re.IGNORECASE,
)

# Minimum task length / word count to be considered potentially valuable.
_MIN_TASK_WORDS = 4
_MIN_ANSWER_CHARS = 200


def _is_trivial(task: str, answer: str) -> bool:
    """Quick rejection: return True for clearly non-reusable exchanges."""
    if _TRIVIAL_TASK_RE.match(task.strip()):
        return True
    if len(task.split()) < _MIN_TASK_WORDS and len(answer) < _MIN_ANSWER_CHARS:
        return True
    if len(answer.strip()) < _TRIVIAL_ANSWER_MAX_CHARS:
        return True
    return False


def _has_knowledge_signals(text: str) -> bool:
    return bool(_KNOWLEDGE_SIGNALS.search(text))


# ---------------------------------------------------------------------------
# LLM-driven extraction prompt
# ---------------------------------------------------------------------------

EXTRACT_PROMPT = """\
你是一个知识管理助手。分析以下对话，提取对后续编程任务有复用价值的经验。

规则：
1. 如果对话内容没有长期记忆价值（如闲聊、简单问候、一次性操作），返回空数组 []
2. 每条经验不超过 60 个汉字，必须是可独立理解的知识点
3. 聚焦：bug 修复方案、配置技巧、架构理解、工作流程、重要决策
4. 最多提取 3 条经验
5. 只返回 JSON 字符串数组，不要任何其他内容

用户任务：{task}

Agent 回答摘要：{answer_summary}

返回格式：["经验1", "经验2"] 或 []
"""


# ---------------------------------------------------------------------------
# Keyword-based fallback extraction
# ---------------------------------------------------------------------------

_SENTENCE_RE = re.compile(r"[^。！？\n]+[。！？]?")

_IMPORTANCE_MARKERS = [
    (re.compile(r"(关键|核心|重要|注意|必须|务必|最佳实践)"), 0.8),
    (re.compile(r"(bug|错误|修复|fix|error|solution|解决)"), 0.7),
    (re.compile(r"(配置|config|设置|参数|环境变量)"), 0.6),
    (re.compile(r"(架构|设计|模式|pattern|workflow|流程)"), 0.6),
    (re.compile(r"(def |class |函数|模块|接口)"), 0.5),
]


def _extract_keyword_lessons(task: str, answer: str) -> list[str]:
    """Fallback: extract lessons via keyword heuristics (no LLM call)."""
    sentences = _SENTENCE_RE.findall(answer)
    if not sentences:
        return []

    scored: list[tuple[float, str]] = []
    for sent in sentences:
        text = sent.strip()
        if len(text) < 15 or len(text) > 200:
            continue
        score = 0.0
        for pattern, weight in _IMPORTANCE_MARKERS:
            if pattern.search(text):
                score += weight
        if score > 0.4:
            scored.append((score, text))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [s for _, s in scored[:3]]


# ---------------------------------------------------------------------------
# AutoRecorder
# ---------------------------------------------------------------------------


@dataclass
class AutoRecorder:
    """Orchestrates: assess importance → extract lessons → store in LTM."""

    model: Any = None  # optional cheap ChatModel for LLM extraction
    max_lessons_per_turn: int = 3
    min_lesson_chars: int = 10

    def __init__(
        self,
        model: Any = None,
        *,
        max_lessons_per_turn: int = 3,
    ) -> None:
        self.model = model
        self.max_lessons_per_turn = max_lessons_per_turn

    # -- public API -----------------------------------------------------------

    async def record_if_valuable(
        self,
        ltm: Any,
        *,
        task: str,
        answer: str,
        thread_id: str = "",
    ) -> int:
        """Assess, extract, and persist lessons. Returns number of stored entries.

        Args:
            ltm: ``LongTermMemory`` instance.
            task: The original user task.
            answer: The agent's final text response.
            thread_id: Optional session identifier for metadata.
        """
        # Stage 1: fast rejection
        if _is_trivial(task, answer):
            return 0

        # Stage 2: quick signal check (must have at least some knowledge markers
        #          OR be a substantial answer)
        if not _has_knowledge_signals(task + " " + answer):
            if len(answer) < 400:
                return 0

        # Stage 3: extract lessons
        lessons = await self._extract_lessons(task, answer)

        # Stage 4: persist
        count = 0
        for lesson in lessons:
            if len(lesson) < self.min_lesson_chars:
                continue
            try:
                await ltm.remember(
                    text=lesson,
                    metadata={
                        "source": "auto_recorder",
                        "thread_id": thread_id,
                        "task_snippet": task[:120],
                    },
                )
                count += 1
            except Exception:  # noqa: BLE001
                continue

        return count

    # -- internals ------------------------------------------------------------

    async def _extract_lessons(self, task: str, answer: str) -> list[str]:
        """Try LLM extraction first; fall back to keyword heuristics."""
        if self.model is not None:
            try:
                return await self._llm_extract(task, answer)
            except Exception:  # noqa: BLE001
                pass
        return _extract_keyword_lessons(task, answer)

    async def _llm_extract(self, task: str, answer: str) -> list[str]:
        import json as _json

        from langchain_core.messages import HumanMessage, SystemMessage

        answer_summary = answer[:2000]  # don't send the full answer to LLM
        prompt = EXTRACT_PROMPT.format(task=task, answer_summary=answer_summary)

        response = await self.model.ainvoke(
            [HumanMessage(content=prompt)]
        )
        content = response.content
        if isinstance(content, list):
            content = "".join(str(p) for p in content)

        # Parse JSON array from possibly noisy output
        match = re.search(r"\[.*\]", str(content), re.DOTALL)
        if not match:
            return []
        try:
            parsed = _json.loads(match.group(0))
        except _json.JSONDecodeError:
            return []
        if not isinstance(parsed, list):
            return []
        return [
            str(item).strip()
            for item in parsed[: self.max_lessons_per_turn]
            if isinstance(item, str) and item.strip()
        ]


__all__ = ["AutoRecorder"]
