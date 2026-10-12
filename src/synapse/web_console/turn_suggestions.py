"""Stateless post-turn follow-up action suggestions for the web console.

Runs only after a turn settles (``turn_completed``), completely outside the
LangGraph checkpoint and session transcript so intermediate model steps are
never polluted and prompt caches stay untouched.  Any provider failure or
timeout degrades cleanly to an empty suggestion list (the frontend's
deterministic rule engine already renders baseline actions immediately).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MAX_USER_PROMPT_CHARS = 1000
MAX_CONCLUSION_CHARS = 2500
MAX_LABEL_CHARS = 24
MAX_PROMPT_CHARS = 240
MAX_SUGGESTIONS = 3
SUGGESTION_TIMEOUT_SECONDS = 8.0

_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)
_JSON_ARRAY_RE = re.compile(r"\[[\s\S]*\]")

_SYSTEM_PROMPT = (
    "你是一个编码助手界面的快捷动作提取器。"
    "根据用户的上一条请求和助手的最终结论，给出 1 到 3 个用户接下来最可能点击的快捷跟进动作。\n"
    "只输出 JSON 数组，不要输出任何解释或 Markdown 标题。格式如下：\n"
    '[{"label": "按钮文字(2-12字)", "prompt": "发送给助手的完整指令", '
    '"mode": "submit"}]\n'
    "规则：\n"
    "1. 如果结论中提出了多个方案或询问用户确认，优先为每个方案或确认操作生成具体动作。\n"
    "2. 如果结论完成了代码修改，优先推荐针对性的验证、测试或下一步推进动作。\n"
    "3. mode 只能是 \"submit\"（点击立即发送）或 \"fill\"（填入输入框待用户补充参数）。\n"
    "4. 不要生成空泛无意义的寒暄。"
)


class TurnSuggestionsError(ValueError):
    """Invalid turn-suggestions request parameter (mapped to HTTP 400)."""


def _clean_text(value: Any, *, field_name: str, max_chars: int, required: bool = False) -> str:
    if value is None:
        if required:
            raise TurnSuggestionsError(f"{field_name} is required")
        return ""
    if not isinstance(value, str):
        raise TurnSuggestionsError(f"{field_name} must be a string")
    cleaned = value.strip()
    if required and not cleaned:
        raise TurnSuggestionsError(f"{field_name} must not be empty")
    if len(cleaned) > max_chars:
        return cleaned[:max_chars]
    return cleaned


def _resolve_workspace(
    default_workspace: Path,
    project_id: str | None,
    project_entries: Sequence[Mapping[str, Any]] | None,
) -> Path:
    if project_id and project_entries:
        for entry in project_entries:
            if entry.get("project_id") == project_id:
                raw_path = entry.get("workspace_path")
                if isinstance(raw_path, str) and raw_path.strip():
                    candidate = Path(raw_path).expanduser()
                    try:
                        resolved = candidate.resolve()
                        if resolved.is_dir():
                            return resolved
                    except OSError:
                        break
    return Path(default_workspace).expanduser().resolve()


def parse_model_suggestions_json(raw_text: str) -> list[dict[str, str]]:
    """Extract and sanitize a bounded list of suggestion dicts from raw LLM output."""
    text = (raw_text or "").strip()
    if not text:
        return []
    fence_match = _FENCE_RE.search(text)
    candidate = fence_match.group(1).strip() if fence_match else text
    if not candidate.startswith("["):
        arr_match = _JSON_ARRAY_RE.search(candidate)
        if arr_match:
            candidate = arr_match.group(0)
    try:
        parsed = json.loads(candidate)
    except ValueError:
        return []
    if not isinstance(parsed, list):
        return []

    results: list[dict[str, str]] = []
    seen_labels: set[str] = set()
    for item in parsed:
        if not isinstance(item, dict):
            continue
        raw_label = item.get("label")
        raw_prompt = item.get("prompt")
        raw_mode = item.get("mode", "submit")
        if not isinstance(raw_label, str) or not isinstance(raw_prompt, str):
            continue
        label = " ".join(raw_label.split()).strip()
        prompt = raw_prompt.strip()
        if not label or not prompt:
            continue
        if len(label) > MAX_LABEL_CHARS:
            label = label[: MAX_LABEL_CHARS - 1].rstrip() + "…"
        if len(prompt) > MAX_PROMPT_CHARS:
            prompt = prompt[:MAX_PROMPT_CHARS].rstrip()
        norm_key = label.lower()
        if norm_key in seen_labels:
            continue
        seen_labels.add(norm_key)
        mode = "fill" if raw_mode == "fill" else "submit"
        results.append(
            {
                "label": label,
                "prompt": prompt,
                "mode": mode,
            }
        )
        if len(results) >= MAX_SUGGESTIONS:
            break
    return results


async def _default_model_invoker(
    workspace: Path,
    model_alias: str | None,
    user_prompt: str,
    conclusion: str,
) -> str:
    """Invoke the project's configured chat model with a short timeout."""
    from langchain_core.messages import HumanMessage, SystemMessage

    from synapse.models.registry import registry_from_settings
    from synapse.settings.schema import load_settings

    def _build_model() -> Any:
        settings = load_settings(project_root=workspace)
        reg = registry_from_settings(settings)
        chosen = model_alias or settings.active_model
        return reg.build_chat_model(chosen)

    chat_model = await asyncio.to_thread(_build_model)
    human_body = (
        f"【用户请求】\n{user_prompt or '(无)'}\n\n"
        f"【助手结论】\n{conclusion}"
    )
    response = await asyncio.wait_for(
        chat_model.ainvoke(
            [
                SystemMessage(content=_SYSTEM_PROMPT),
                HumanMessage(content=human_body),
            ]
        ),
        timeout=SUGGESTION_TIMEOUT_SECONDS,
    )
    content = getattr(response, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "\n".join(parts)
    return str(content or "")


async def generate_turn_suggestions(
    workspace: Path,
    *,
    payload: Mapping[str, Any],
    project_entries: Sequence[Mapping[str, Any]] | None = None,
    model_invoker: Callable[[Path, str | None, str, str], Awaitable[str]] | None = None,
) -> dict[str, Any]:
    """Validate payload and return bounded model-generated follow-up suggestions."""
    if not isinstance(payload, Mapping):
        raise TurnSuggestionsError("payload must be a JSON object")

    user_prompt = _clean_text(
        payload.get("user_prompt"),
        field_name="user_prompt",
        max_chars=MAX_USER_PROMPT_CHARS,
        required=False,
    )
    conclusion = _clean_text(
        payload.get("conclusion"),
        field_name="conclusion",
        max_chars=MAX_CONCLUSION_CHARS,
        required=False,
    )
    project_id = _clean_text(
        payload.get("project_id"),
        field_name="project_id",
        max_chars=128,
        required=False,
    )
    model_alias = _clean_text(
        payload.get("model"),
        field_name="model",
        max_chars=128,
        required=False,
    )

    if not conclusion:
        return {"suggestions": []}

    target_workspace = _resolve_workspace(
        workspace,
        project_id or None,
        project_entries,
    )
    invoker = model_invoker or _default_model_invoker
    try:
        raw_output = await invoker(
            target_workspace,
            model_alias or None,
            user_prompt,
            conclusion,
        )
    except Exception as exc:  # noqa: BLE001 - best-effort side-channel; never fail UI
        logger.debug("turn suggestions model call skipped/failed: %s", type(exc).__name__)
        return {"suggestions": []}

    suggestions = parse_model_suggestions_json(raw_output)
    return {"suggestions": suggestions}
