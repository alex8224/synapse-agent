"""Goal 记账 middleware：模型调用后累计 token 并结算进度。

对应 Codex ``on_token_usage`` + ``on_tool_finish`` 钩子的合并简化：
- ``before_model``：确保该 thread 有活跃回合（回合按用户消息指纹轮换）。
- ``after_model``：累计本次模型调用 usage 并立即结算一次（token + 时间
  增量写入持久化，预算耗尽时自动置 budget_limited）。

回合结束的最终结算与自动继续由 UI 层（TUI ``_turn_done`` / CLI 回合结束）
调用 :meth:`GoalService.on_turn_end` 完成。
"""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware import AgentMiddleware, AgentState
from langgraph.config import get_config

from synapse.goals.runtime import get_goal_service


def _config_thread_id() -> str | None:
    try:
        config = get_config()
    except Exception:  # noqa: BLE001 - 非 runnable 上下文时跳过
        return None
    configurable = dict((config or {}).get("configurable") or {})
    tid = configurable.get("thread_id")
    return str(tid) if tid else None


def _usage_from_response(response: Any) -> tuple[int, int, int]:
    """从模型响应提取 (input_tokens, output_tokens, cache_read_tokens)。"""
    messages = list(getattr(response, "result", None) or [])
    input_tokens = 0
    output_tokens = 0
    cache_read = 0
    for message in messages:
        usage = getattr(message, "usage_metadata", None) or {}
        if not isinstance(usage, dict):
            usage = {
                "input_tokens": getattr(usage, "input_tokens", 0),
                "output_tokens": getattr(usage, "output_tokens", 0),
                "input_token_details": getattr(usage, "input_token_details", None),
            }
        input_tokens += int(usage.get("input_tokens", 0) or 0)
        output_tokens += int(usage.get("output_tokens", 0) or 0)
        details = usage.get("input_token_details") or {}
        if not isinstance(details, dict):
            details = vars(details) if hasattr(details, "__dict__") else {}
        cache_read += int(
            details.get("cache_read", 0)
            or details.get("cache_read_tokens", 0)
            or details.get("cached_tokens", 0)
            or 0
        )
    return input_tokens, output_tokens, cache_read


def build_goal_middleware(enabled: bool = True):
    """构建 goal 记账 middleware。

    每个模型调用边界结算一次（``wrap_model_call`` 包住 handler，从响应提取
    usage 并结算）；goal 未启用时是纯 no-op。
    """
    if not enabled:
        return type("goal_middleware_disabled", (AgentMiddleware,), {})(
            state_schema=AgentState
        )

    def _before_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:  # noqa: ARG001
        service = get_goal_service()
        thread_id = _config_thread_id()
        if service is None or not thread_id:
            return None
        try:
            service.on_model_call_begin(thread_id)
        except Exception:  # noqa: BLE001
            pass
        return None

    async def _abefore_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        return _before_model(self, state, runtime)

    def _settle(response: Any) -> None:
        service = get_goal_service()
        thread_id = _config_thread_id()
        if service is None or not thread_id:
            return
        try:
            input_tokens, output_tokens, cache_read = _usage_from_response(response)
            service.on_model_call_end(
                thread_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read,
            )
        except Exception:  # noqa: BLE001 - 记账失败不阻断主流程
            pass

    def _wrap_model_call(self, request: Any, handler: Any) -> Any:  # noqa: ARG001
        response = handler(request)
        _settle(response)
        return response

    async def _awrap_model_call(self, request: Any, handler: Any) -> Any:
        response = await handler(request)
        _settle(response)
        return response

    return type(
        "goal_accounting",
        (AgentMiddleware,),
        {
            "state_schema": AgentState,
            "tools": [],
            "before_model": _before_model,
            "abefore_model": _abefore_model,
            "wrap_model_call": _wrap_model_call,
            "awrap_model_call": _awrap_model_call,
        },
    )()
