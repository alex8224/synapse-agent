"""DAG 拓扑排序 + 异步并行子 Agent 调度中间件。

用 AgentMiddleware 方式实现，替换 deepagents 的 SubAgentMiddleware：
- 完全兼容 create_deep_agent / create_agent 的中间件体系
- 所有现有中间件（FilesystemMiddleware、SummarizationMiddleware 等）正常工作
- 只在 awrap_model_call 中拦截 task 调用，执行 DAG 调度
- 子 Agent 异步并行执行（ainvoke + asyncio.gather）

架构：
  create_deep_agent() 构建的图保持不变
      │
      │  中间件栈:
      │    TodoListMiddleware        ✅ 正常
      │    FilesystemMiddleware      ✅ 正常
      │    DAGSubAgentMiddleware     ★ 替换了 SubAgentMiddleware
      │    SummarizationMiddleware   ✅ 正常
      │    PatchToolCallsMiddleware  ✅ 正常
      │    MemoryMiddleware          ✅ 正常
      │
      ▼
  awrap_model_call 拦截模型输出:
      1. 调用模型 → 获取 AIMessage(tool_calls=[...])
      2. 分离 task 调用 vs 普通工具调用
      3. 对 task 调用做拓扑排序 → 波次并行执行
      4. 将子 Agent 结果注入为 ToolMessage
      5. 返回结果（普通工具调用保留，路由到 ToolNode）
"""

from __future__ import annotations

import asyncio
import logging
import re as _re
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
)
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════
# task 工具参数 schema（支持依赖声明）
# ═══════════════════════════════════════════════════════════


class DAGTaskArgs(BaseModel):
    """并行 task 工具的参数——比标准 task 多了 task_id 和 depends_on。"""

    subagent_type: str = Field(
        description="要调用的子 Agent 名称，如 tester / reviewer / researcher"
    )
    description: str = Field(
        description="任务的详细描述，子 Agent 会基于这个描述来执行"
    )
    task_id: str = Field(
        default="",
        description=(
            "任务的唯一短标识。如果留空则自动生成。"
            "当其他任务需要依赖此任务时，用此 ID 来引用。"
            "建议使用有意义的短名，如 'test'、'review'、'doc'。"
        ),
    )
    depends_on: list[str] = Field(
        default_factory=list,
        description=(
            "此任务依赖的其他 task_id 列表。"
            "这些依赖任务全部完成后，本任务才会开始执行。"
            "依赖任务的输出会自动作为上下文注入到本任务的描述中。"
            "无依赖的任务会在第一波并行执行。"
        ),
    )


# ═══════════════════════════════════════════════════════════
# DAG 调度提示词
# ═══════════════════════════════════════════════════════════

DAG_USAGE_INSTRUCTIONS = """
## 并行子 Agent 调度（DAG 模式）

你可以一次性调用多个 task() 来并行委派子 Agent。对于有依赖关系的任务，
使用 depends_on 参数声明执行顺序：

**无依赖任务**（第一波并行）:
  task("researcher", "搜索认证相关代码", task_id="search")
  task("writer", "草拟 API 文档大纲", task_id="doc")

**有依赖任务**（等依赖完成后再启动）:
  task("tester", "基于搜索结果写测试", depends_on=["search"], task_id="test")
  task("reviewer", "审查测试和文档", depends_on=["test", "doc"], task_id="review")

上面 4 个调用：
- 第一波: search + doc 并行执行
- 第二波: test 启动（等 search 完成）
- 第三波: review 启动（等 test 和 doc 都完成）

**关键规则**:
- 相互独立的子任务务必在同一轮 task() 调用中一次性发出
- 不要等一个 task() 完成后才发下一个——那会退化为串行
- task_id 只需要在 depends_on 中被引用时才需要指定
- depends_on 错误（循环依赖、不存在的 ID）会触发报错

**执行语义（非常重要）**:
- task() 是同步阻塞调用：同一轮发出的 task 会在本轮内全部执行完，再以 ToolMessage 返回最终结果
- 子 Agent 不会在后台继续跑；收到 task ToolMessage 后立刻整合，禁止输出“正在等待子 Agent”
- 不要把“计划开始阅读/即将分析”当成完成结果；只有带证据的最终结论才算完成
"""


# ═══════════════════════════════════════════════════════════
# 从 deepagents SubAgent 规范预编译 Runnable
# ═══════════════════════════════════════════════════════════

def _build_subagent_middleware(
    *,
    model: BaseChatModel | None,
    backend: Any | None,
    extra_middleware: list[Any] | None = None,
) -> list[Any]:
    """Mirror deepagents inline-subagent middleware assembly for DAG mode.

    Without FilesystemMiddleware + backend, subagents compile as empty shells
    (no read_file/glob/execute), which makes parallel research appear hung.
    """
    middleware: list[Any] = []
    if backend is not None:
        try:
            from langchain.agents.middleware import TodoListMiddleware

            middleware.append(TodoListMiddleware())
        except Exception:  # noqa: BLE001
            logger.debug("TodoListMiddleware unavailable for DAG subagent", exc_info=True)

        try:
            from deepagents.middleware.filesystem import FilesystemMiddleware

            middleware.append(FilesystemMiddleware(backend=backend))
        except Exception:  # noqa: BLE001
            logger.warning("FilesystemMiddleware unavailable for DAG subagent", exc_info=True)

        if model is not None:
            try:
                from deepagents.middleware.summarization import create_summarization_middleware

                middleware.append(create_summarization_middleware(model, backend))
            except Exception:  # noqa: BLE001
                logger.debug("summarization middleware skipped for DAG subagent", exc_info=True)

        try:
            from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware

            middleware.append(PatchToolCallsMiddleware())
        except Exception:  # noqa: BLE001
            logger.debug("PatchToolCallsMiddleware unavailable for DAG subagent", exc_info=True)

    if extra_middleware:
        middleware.extend(list(extra_middleware))
    return middleware


def compile_subagent_runnables(
    subagent_specs: list[dict[str, Any]],
    *,
    default_model: BaseChatModel | None = None,
    backend: Any | None = None,
    default_tools: list[Any] | None = None,
) -> dict[str, Runnable]:
    """将 SubAgent 规范列表预编译为 {name: CompiledGraph} 映射。

    每个规范被编译为独立的 CompiledStateGraph，
    在 DAG 调度时通过 .ainvoke() 异步执行。

    关键：必须尽量复刻 deepagents.create_deep_agent 对 inline subagent 的装配，
    尤其是 FilesystemMiddleware(backend=...)。否则子 Agent 只有空 tools，
    会表现为“已调度但无子工具 / Smith 无记录 / 结果像在等待”。

    参数:
        subagent_specs: deepagents SubAgent 规范列表
        default_model: 规范未指定 model 时使用的默认模型
        backend: 与主 Agent 共享的 backend（提供 ls/read_file/glob/execute）
        default_tools: 规范未显式声明 tools 时继承的父工具列表

    返回:
        {"tester": CompiledGraph, "reviewer": CompiledGraph, ...}
    """
    from deepagents.middleware.subagents import create_sub_agent

    compiled: dict[str, Runnable] = {}
    inherited_tools = list(default_tools or [])

    for spec in subagent_specs:
        name = spec.get("name", "unknown")
        completed: dict[str, Any] = dict(spec)
        model = completed.get("model", default_model)
        if "model" not in completed and default_model is not None:
            completed["model"] = default_model

        # Match deepagents graph.py:
        #   tools = spec.tools if "tools" in spec else parent_tools
        if "tools" in completed:
            completed["tools"] = list(completed.get("tools") or [])
        else:
            completed["tools"] = list(inherited_tools)

        completed["middleware"] = _build_subagent_middleware(
            model=model if hasattr(model, "invoke") or hasattr(model, "ainvoke") else default_model,
            backend=backend,
            extra_middleware=list(spec.get("middleware") or []),
        )

        try:
            result = create_sub_agent(completed)
            # 兼容 deepagents 新旧版本返回格式
            if isinstance(result, dict) and "runnable" in result:
                compiled[name] = result["runnable"]
            elif hasattr(result, "ainvoke"):
                # 新版本直接返回 CompiledStateGraph
                compiled[name] = result
            else:
                logger.warning(
                    "编译子 Agent '%s' 返回未知类型: %s", name, type(result)
                )
        except Exception:
            logger.warning("编译子 Agent '%s' 失败，跳过", name, exc_info=True)

    return compiled


# ═══════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════

# 匹配类 XML 工具调用片段（模型可能以文本形式输出工具调用 DSL）
# 策略：先移除成对的同名标签块，再移除孤立标签片段，最后清洗空白

# 匹配成对的标准 XML 标签块（同名开闭标签），如 <tool_calls>...</tool_calls>
_PAIRED_XML_RE = _re.compile(
    r"<\s*(tool_calls?|function_calls?|invoke)\b[^>]*>"
    r".*?</\s*\1\s*>",
    _re.DOTALL | _re.IGNORECASE,
)

# 匹配 DSML 管道格式标签片段（无同名闭标签保证，逐标签移除）
# 包括：< | | DSML | | tool_calls>, </ | | DSML | | invoke>, 等
_DSML_TAG_RE = _re.compile(
    r"</?\s*\|\s*\|\s*DSML\s*\|\s*\|\s+"
    r"(?:tool_calls?|function_calls?|invoke|parameter|arg)\b[^>]*/?>",
    _re.IGNORECASE,
)

# 匹配自闭合标签：<parameter ... />, <arg ... />
_SELF_CLOSING_XML_RE = _re.compile(
    r"<\s*(?:parameter|arg)\b[^>]*?/>",
    _re.IGNORECASE,
)

# 匹配残留的标准 XML 标签片段（未被成对匹配清除的）
_STANDALONE_XML_TAG_RE = _re.compile(
    r"</?\s*(?:tool_calls?|function_calls?|invoke|parameter|arg)\b[^>]*/?>",
    _re.IGNORECASE,
)


def _strip_tool_call_xml(text: str) -> str:
    """清洗文本中可能混入的 XML 工具调用 DSL 片段。

    部分模型在流式输出时会将工具调用以类 XML 文本形式写入 content 字段。
    此函数移除这些片段，保留纯文本回复。

    清洗步骤：
    1. 移除成对的标准 XML 块（同名标签）
    2. 移除 DSML 管道格式标签
    3. 移除自闭合标签
    4. 移除残留的孤立标准 XML 标签
    5. 合并多余空行
    """
    if not text:
        return text
    cleaned = _PAIRED_XML_RE.sub("", text)
    cleaned = _DSML_TAG_RE.sub("", cleaned)
    cleaned = _SELF_CLOSING_XML_RE.sub("", cleaned)
    cleaned = _STANDALONE_XML_TAG_RE.sub("", cleaned)
    # 合并多余空行（>2 连续空行 → 1 空行）
    cleaned = _re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _extract_final_response(messages: list) -> str:
    """从子 Agent 的消息列表中提取最终 AI 回复文本。

    优先返回最后一条无 tool_calls 的 AIMessage 文本内容（即最终回复）。
    如果全部 AIMessage 都携带 tool_calls，回退到清洗后的文本内容。
    """
    # 第一轮：找最后一条无 tool_calls 且有内容的 AIMessage（最终回复）
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            if getattr(msg, "tool_calls", None):
                continue
            content = msg.content or ""
            if isinstance(content, str) and content.strip():
                return _strip_tool_call_xml(content)
            if isinstance(content, list):
                # Anthropic content_blocks 格式：拼接 text 类型块
                text_parts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text_parts.append(str(block.get("text", "")))
                    elif isinstance(block, str):
                        text_parts.append(block)
                joined = "".join(text_parts).strip()
                if joined:
                    return _strip_tool_call_xml(joined)

    # 第二轮（回退）：全部 AIMessage 都含 tool_calls，取最后一条有内容的消息并清洗
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            content = msg.content or ""
            if isinstance(content, str) and content.strip():
                return _strip_tool_call_xml(content)
            if isinstance(content, list):
                text_parts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text_parts.append(str(block.get("text", "")))
                    elif isinstance(block, str):
                        text_parts.append(block)
                joined = "".join(text_parts).strip()
                if joined:
                    return _strip_tool_call_xml(joined)

    # 第三轮：若模型几乎没给出有效最终文本，汇总工具错误，避免父 Agent 误判“还在跑”
    tool_errors: list[str] = []
    for msg in messages:
        if isinstance(msg, ToolMessage):
            content = str(getattr(msg, "content", "") or "").strip()
            name = str(getattr(msg, "name", "") or "tool")
            lowered = content.lower()
            if content and (
                content.startswith("Error:")
                or "is not a valid tool" in lowered
                or "traceback" in lowered
            ):
                tool_errors.append(f"- {name}: {content[:500]}")
    if tool_errors:
        return (
            "子 Agent 未能完成有效分析，工具执行出现错误：\n"
            + "\n".join(tool_errors[:8])
        )

    return "(子 Agent 未返回文本回复)"


def _enrich_description(task: dict, upstream_results: dict[str, str]) -> str:
    """将上游依赖任务的输出注入到下游任务的描述中。"""
    base_desc: str = task.get("description", "")
    depends_on: list[str] = task.get("depends_on", [])

    if not depends_on:
        return base_desc

    context_parts: list[str] = []
    for dep_id in depends_on:
        if dep_id in upstream_results:
            context_parts.append(
                f"## 依赖任务 [{dep_id}] 的输出\n\n{upstream_results[dep_id]}"
            )

    if not context_parts:
        return base_desc

    return (
        "\n\n".join(context_parts)
        + "\n\n---\n\n## 你的任务\n\n"
        + base_desc
    )


def _format_dag_results(
    results: dict[str, str],
    task_wave: dict[str, int],
    task_deps: dict[str, list[str]],
    total_waves: int,
    task_type_map: dict[str, str],
) -> str:
    """将 DAG 执行结果格式化为结构化展示文本。

    参数:
        results: {task_id: result_text}
        task_wave: {task_id: wave_number}
        task_deps: {task_id: [dep_task_id, ...]}
        total_waves: 总波次数
        task_type_map: {task_id: subagent_type}

    返回:
        格式化后的多段落文本
    """
    if not results:
        return "(DAG 未产生任何结果)"

    # 按波次分组
    waves: dict[int, list[tuple[str, str, list[str]]]] = {}
    for tid, w in task_wave.items():
        waves.setdefault(w, []).append((tid, task_type_map.get(tid, "?"), task_deps.get(tid, [])))

    lines: list[str] = []
    lines.append(f"DAG 执行完成: {total_waves} 个波次, {len(results)} 个任务")
    lines.append("")

    for w in sorted(waves):
        items = waves[w]
        is_parallel = len(items) > 1
        wave_label = f"波次 {w}" + (" (并行)" if is_parallel else "")
        if w == 1 and all(not deps for _, _, deps in items):
            lines.append(f"-- {wave_label} (无依赖) --")
        else:
            lines.append(f"-- {wave_label} --")

        for tid, stype, deps in items:
            dep_str = f" [依赖: {', '.join(deps)}]" if deps else ""
            lines.append(f"  {tid} ({stype}){dep_str}")
        lines.append("")

    lines.append("=" * 50)

    for tid in task_wave:
        stype = task_type_map.get(tid, "?")
        w = task_wave.get(tid, "?")
        deps = task_deps.get(tid, [])
        dep_str = f", 依赖: {', '.join(deps)}" if deps else ""

        lines.append("")
        lines.append(f"[{stype}] {tid} (波次 {w}{dep_str})")
        lines.append("-" * 40)
        lines.append(results.get(tid, "(无结果)"))
        lines.append("")

    return "\n".join(lines)


def _topological_waves(
    pending: list[dict],
    completed: set[str],
) -> tuple[list[dict], list[dict]]:
    """拓扑排序：将 pending 任务分为"可执行波次"和"等待波次"。

    参数:
        pending: 待处理任务
        completed: 已完成的 task_id 集合

    返回:
        (ready, remaining)
        ready: 所有依赖已满足，当前可以并行执行的任务
        remaining: 还有未满足依赖，需要等待的任务
    """
    ready: list[dict] = []
    remaining: list[dict] = []

    for task in pending:
        deps: list[str] = task.get("depends_on", [])
        unmet = [d for d in deps if d not in completed]
        if not unmet:
            ready.append(task)
        else:
            remaining.append(task)

    # 不在这里检测死锁——留给 _execute_dag 的 while 循环处理。
    # 如果 ready 持续为空且 remaining 非空，_execute_dag 会 detect 并报错。

    return ready, remaining


# ═══════════════════════════════════════════════════════════
# DAGSubAgentMiddleware —— 核心中间件
# ═══════════════════════════════════════════════════════════

class DAGSubAgentMiddleware(AgentMiddleware):
    """替换 SubAgentMiddleware，支持 DAG 拓扑排序并行调度。

    工作原理：
    1. 暴露一个增强版 task 工具（schema 含 depends_on / task_id）
    2. 在 awrap_model_call 中拦截模型输出
    3. 提取所有 task() 调用 → 拓扑排序 → 波次并行执行
    4. 将子 Agent 结果以 ToolMessage 形式注入
    5. 非 task 的工具调用保持不变，正常路由到 ToolNode

    使用方式：
        # 用 DAGSubAgentMiddleware 替代 SubAgentMiddleware
        agent = create_deep_agent(
            model=model,
            middleware=[
                FilesystemMiddleware(backend=backend),
                DAGSubAgentMiddleware(subagents=subagents),  # ← 替换这里
                SummarizationMiddleware(model=model),
            ],
        )
    """

    name = "DAGSubAgentMiddleware"

    def __init__(
        self,
        *,
        subagents: list[dict[str, Any]],
        default_model: BaseChatModel | None = None,
        backend: Any | None = None,
        default_tools: list[Any] | None = None,
        max_parallel: int = 6,
        task_description: str | None = None,
    ) -> None:
        """初始化 DAG 子 Agent 中间件。

        参数:
            subagents: SubAgent 规范列表（与 deepagents SubAgentMiddleware 兼容）
            default_model: 子 Agent 默认使用的模型
            backend: 与主 Agent 共享的 backend（注入 filesystem/execute 工具）
            default_tools: 子 Agent 未声明 tools 时继承的父工具
            max_parallel: 每波最多并行几个子 Agent
            task_description: task 工具的自定义描述
        """
        super().__init__()
        self._subagent_specs = subagents
        self._max_parallel = max_parallel
        self._backend = backend
        self._dag_cache: dict[str, str] = {}  # {tool_call_id: result_text}
        self._parent_run_config: dict[str, Any] | None = None

        # 预编译所有子 Agent（必须带 backend，否则无 read_file/glob）
        self._subagent_runnables = compile_subagent_runnables(
            subagents,
            default_model=default_model,
            backend=backend,
            default_tools=default_tools,
        )

        # 构建系统提示词
        agents_desc = "\n".join(
            f"- {s['name']}: {s.get('description', '')}" for s in subagents
        )
        self.system_prompt = (
            "可用的子 Agent 类型:\n\n" + agents_desc + "\n\n" + DAG_USAGE_INSTRUCTIONS
        )

        # 构建增强版 task 工具（带依赖声明 schema）
        task_desc = task_description or (
            "委派任务给子 Agent 执行。支持并行调度和依赖声明。\n\n"
            "可用子 Agent:\n" + agents_desc
        )
        self.tools = [
            StructuredTool.from_function(
                name="task",
                func=self._task_placeholder,
                coroutine=self._atask_placeholder,
                description=task_desc,
                args_schema=DAGTaskArgs,
            )
        ]

    # ── 工具占位函数（不会真正被 ToolNode 调用，只提供 schema）──

    def _task_placeholder(
        self,
        subagent_type: str,
        description: str,
        task_id: str = "",
        depends_on: list[str] | None = None,
    ) -> str:
        """同步占位——实际执行在 awrap_model_call 中。"""
        return "[DAG] 任务已调度，稍后执行"

    async def _atask_placeholder(
        self,
        subagent_type: str,
        description: str,
        task_id: str = "",
        depends_on: list[str] | None = None,
    ) -> str:
        """异步占位——实际执行在 awrap_model_call 中。"""
        return "[DAG] 任务已调度，稍后执行"

    # ── 核心 Hook 1: awrap_model_call —— 执行 DAG，缓存结果 ──

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """拦截异步模型调用。

        1. 调用模型 → 获取 AIMessage（含所有 tool_calls）
        2. 提取 task 调用 → 执行 DAG 拓扑排序并行调度
        3. 将结果缓存到 self._dag_cache[tool_call_id]
        4. ★ 返回模型输出不变（AIMessage 保留全部 tool_calls）
           → API 校验通过（ToolMessage 需匹配 AIMessage 的 tool_calls）
           → 后续 ToolNode 处理时由 awrap_tool_call 返回缓存结果
        """
        if self.system_prompt:
            new_system = _append_system_message(
                request.system_message, self.system_prompt
            )
            request = request.override(system_message=new_system)

        response = await handler(request)

        # —— 检查是否有 task 调用 ——
        messages = response.result
        if not messages:
            return response

        last_msg = messages[-1] if messages else None
        if not isinstance(last_msg, AIMessage):
            return response

        tool_calls = getattr(last_msg, "tool_calls", None)
        if not tool_calls:
            return response

        task_calls = [tc for tc in tool_calls if tc.get("name") == "task"]
        if not task_calls:
            return response

        # —— 捕获父 run config，供子 Agent tracing / namespace 传播 ——
        self._parent_run_config = _capture_parent_run_config(request)

        # —— 执行 DAG 并行调度 ——
        dag_results, task_wave, task_deps, total_waves = await self._execute_dag(task_calls)

        # 构建 task_id → subagent_type 映射
        task_type_map: dict[str, str] = {}
        for tc in task_calls:
            args: dict = tc.get("args", {})
            tid = args.get("task_id", "unknown")
            task_type_map[tid] = args.get("subagent_type", "unknown")

        # —— 缓存结果，供 awrap_tool_call 使用 ——
        for tc in task_calls:
            tc_id = tc.get("id", "")
            args: dict = tc.get("args", {})
            task_id = args.get("task_id", "unknown")
            subagent_type = args.get("subagent_type", "unknown")

            # 单个任务结果：简洁格式
            w = task_wave.get(task_id, "?")
            deps = task_deps.get(task_id, [])
            dep_hint = f" (依赖: {', '.join(deps)})" if deps else ""
            result_text = dag_results.get(task_id, "(无结果)")

            tool_content = (
                f"[DAG 波次 {w}: {subagent_type} | {task_id}{dep_hint}]\n\n"
                f"{result_text}"
            )
            self._dag_cache[tc_id] = tool_content

        # ★ 返回原始 AIMessage（tool_calls 完整保留）
        #   → ToolNode 会在下一步处理，awrap_tool_call 返回缓存结果
        return response

    # ── 核心 Hook 2: awrap_tool_call —— 返回缓存结果 ──

    async def awrap_tool_call(
        self,
        request: Any,
        handler: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        """拦截 task 工具调用，返回 DAG 预计算的缓存结果。

        非 task 工具 → 正常执行。
        task 工具 → 从缓存中取出结果直接返回（不执行子 Agent）。
        """
        tc_name = getattr(request, "tool_call", {}).get("name", "")
        if tc_name != "task":
            return await handler(request)

        tc_id = getattr(request, "tool_call", {}).get("id", "")
        cached = self._dag_cache.pop(tc_id, None)

        if cached is not None:
            return ToolMessage(
                content=cached,
                tool_call_id=tc_id,
                name="task",
            )

        # 缓存未命中（异常情况）→ 返回占位消息
        return ToolMessage(
            content="(DAG 调度结果未找到，请重试)",
            tool_call_id=tc_id,
            name="task",
        )

    # ── 核心 Hook 3: wrap_model_call —— 同步版本 ──

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """同步模型调用拦截——内部用 asyncio.run 执行 DAG，缓存结果。

        与 awrap_model_call 逻辑相同，只是用同步方式调用。
        """
        if self.system_prompt:
            new_system = _append_system_message(
                request.system_message, self.system_prompt
            )
            request = request.override(system_message=new_system)

        response = handler(request)

        messages = response.result
        if not messages:
            return response

        last_msg = messages[-1] if messages else None
        if not isinstance(last_msg, AIMessage):
            return response

        tool_calls = getattr(last_msg, "tool_calls", None)
        if not tool_calls:
            return response

        task_calls = [tc for tc in tool_calls if tc.get("name") == "task"]
        if not task_calls:
            return response

        self._parent_run_config = _capture_parent_run_config(request)

        dag_results, task_wave, task_deps, total_waves = asyncio.run(
            self._execute_dag(task_calls)
        )

        # 构建 task_id → subagent_type 映射
        task_type_map: dict[str, str] = {}
        for tc in task_calls:
            args: dict = tc.get("args", {})
            tid = args.get("task_id", "unknown")
            task_type_map[tid] = args.get("subagent_type", "unknown")

        for tc in task_calls:
            tc_id = tc.get("id", "")
            args: dict = tc.get("args", {})
            task_id = args.get("task_id", "unknown")
            subagent_type = args.get("subagent_type", "unknown")

            w = task_wave.get(task_id, "?")
            deps = task_deps.get(task_id, [])
            dep_hint = f" (依赖: {', '.join(deps)})" if deps else ""
            result_text = dag_results.get(task_id, "(无结果)")

            tool_content = (
                f"[DAG 波次 {w}: {subagent_type} | {task_id}{dep_hint}]\n\n"
                f"{result_text}"
            )
            self._dag_cache[tc_id] = tool_content

        return response

    # ── 核心 Hook 4: wrap_tool_call —— 同步版返回缓存结果 ──

    def wrap_tool_call(
        self,
        request: Any,
        handler: Callable[[Any], Any],
    ) -> Any:
        """同步版 tool call 拦截——返回缓存结果。"""
        tc_name = getattr(request, "tool_call", {}).get("name", "")
        if tc_name != "task":
            return handler(request)

        tc_id = getattr(request, "tool_call", {}).get("id", "")
        cached = self._dag_cache.pop(tc_id, None)

        if cached is not None:
            return ToolMessage(
                content=cached,
                tool_call_id=tc_id,
                name="task",
            )

        return ToolMessage(
            content="(DAG 调度结果未找到，请重试)",
            tool_call_id=tc_id,
            name="task",
        )

    # ── DAG 调度引擎 ──

    async def _execute_dag(
        self, task_calls: list[dict]
    ) -> tuple[dict[str, str], dict[str, int], dict[str, list[str]], int]:
        """执行 DAG 拓扑排序 + 波次并行调度。

        参数:
            task_calls: 模型返回的 task 工具调用列表

        返回:
            (results, task_wave, task_deps, total_waves)
            results: {task_id: result_text}
            task_wave: {task_id: wave_number}（从 1 开始）
            task_deps: {task_id: [dep_task_id, ...]}
            total_waves: 总波次数
        """
        # Create an explicit LangSmith/LangChain parent span so nested subagent
        # graphs are not buried only under the parent model node. DAG execution
        # currently happens inside awrap_model_call (before ToolNode), which
        # otherwise makes Smith look like "task returned text with no children".
        try:
            from langsmith import trace as _ls_trace
        except Exception:  # noqa: BLE001
            _ls_trace = None  # type: ignore[assignment]

        if _ls_trace is None:
            return await self._execute_dag_inner(task_calls)

        with _ls_trace(
            name="dag_subagents",
            run_type="chain",
            inputs={
                "task_count": len(task_calls),
                "task_ids": [
                    (tc.get("args") or {}).get("task_id")
                    or f"auto_task_{i}"
                    for i, tc in enumerate(task_calls)
                ],
                "subagent_types": [
                    (tc.get("args") or {}).get("subagent_type")
                    for tc in task_calls
                ],
            },
            metadata={"ls_agent_type": "dag_scheduler"},
            tags=["dag", "subagent"],
        ) as run_tree:
            results, task_wave, task_deps, total_waves = await self._execute_dag_inner(
                task_calls
            )
            try:
                if run_tree is not None:
                    run_tree.end(outputs={
                        "total_waves": total_waves,
                        "completed_task_ids": list(results.keys()),
                    })
            except Exception:  # noqa: BLE001
                pass
            return results, task_wave, task_deps, total_waves

    async def _execute_dag_inner(
        self, task_calls: list[dict]
    ) -> tuple[dict[str, str], dict[str, int], dict[str, list[str]], int]:
        """Inner DAG engine without observability wrappers."""
        # 解析所有任务
        tasks: list[dict] = []
        for i, tc in enumerate(task_calls):
            args: dict = tc.get("args", {})
            tasks.append({
                "tool_call": tc,
                "tool_call_id": tc.get("id", "") or "",
                "subagent_type": args.get("subagent_type", ""),
                "description": args.get("description", ""),
                "task_id": args.get("task_id") or f"auto_task_{i}",
                "depends_on": args.get("depends_on", []) or [],
            })

        results: dict[str, str] = {}
        task_wave: dict[str, int] = {}
        task_deps: dict[str, list[str]] = {}
        for t in tasks:
            task_deps[t["task_id"]] = list(t["depends_on"])

        remaining: list[dict] = list(tasks)
        wave_num = 0

        while remaining:
            ready, remaining = _topological_waves(remaining, set(results.keys()))

            if not ready:
                if remaining:
                    # 死锁：有任务在等待永远不会完成的依赖
                    deadlocked = [
                        (t.get("task_id", "?"), t.get("depends_on", []))
                        for t in remaining
                    ]
                    msg = (
                        "DAG 死锁：以下任务依赖了不存在的或循环引用的 task_id。"
                        f"死锁任务: {deadlocked}, 已完成: {sorted(results.keys())}"
                    )
                    raise ValueError(msg)
                break

            wave_num += 1
            batch = ready[: self._max_parallel]

            for t in batch:
                task_wave[t["task_id"]] = wave_num

            logger.debug(
                "DAG 波次 %d: 并行执行 %d 个子Agent: %s",
                wave_num,
                len(batch),
                [t["task_id"] for t in batch],
            )

            # ★ 同一波次内异步并行执行
            wave_coros = [
                self._run_one_subagent(task, results) for task in batch
            ]
            wave_outputs = await asyncio.gather(*wave_coros)

            for task, output in zip(batch, wave_outputs, strict=True):
                results[task["task_id"]] = output

        return results, task_wave, task_deps, wave_num

    async def _run_one_subagent(
        self,
        task: dict,
        upstream_results: dict[str, str],
    ) -> str:
        """异步执行单个子 Agent。

        参数:
            task: 任务描述 {subagent_type, description, task_id, depends_on, tool_call_id?}
            upstream_results: 已完成的上游任务结果

        返回:
            子 Agent 的最终回复文本
        """
        subagent_type: str = task.get("subagent_type", "")
        task_id: str = task.get("task_id", "unknown")
        tool_call_id: str = str(task.get("tool_call_id") or "")

        runnable = self._subagent_runnables.get(subagent_type)
        if runnable is None:
            return (
                f"错误: 子 Agent '{subagent_type}' 不存在。"
                f"可用: {', '.join(sorted(self._subagent_runnables.keys()))}"
            )

        # 注入上游依赖的输出
        description = _enrich_description(task, upstream_results)
        subagent_config = _build_subagent_run_config(
            parent_config=self._parent_run_config,
            subagent_type=subagent_type,
            task_id=task_id,
            tool_call_id=tool_call_id,
        )

        try:
            # Align with deepagents SubAgentMiddleware.atask:
            # tag ls_agent_type=subagent and keep parent tracing context.
            try:
                from deepagents.middleware.subagents import _subagent_tracing_context
            except Exception:  # noqa: BLE001
                _subagent_tracing_context = None  # type: ignore[assignment]

            if _subagent_tracing_context is not None:
                with _subagent_tracing_context():
                    result = await runnable.ainvoke(
                        {"messages": [HumanMessage(content=description)]},
                        subagent_config,
                    )
            else:
                result = await runnable.ainvoke(
                    {"messages": [HumanMessage(content=description)]},
                    subagent_config,
                )
            return _extract_final_response(result.get("messages", []))
        except Exception as exc:
            logger.warning(
                "子 Agent '%s' (task_id=%s) 执行失败: %s",
                subagent_type, task_id, exc,
            )
            return f"错误: 子 Agent '{subagent_type}' 执行失败: {exc}"


# ═══════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════

def _capture_parent_run_config(request: Any) -> dict[str, Any] | None:
    """Best-effort capture of the active parent RunnableConfig.

    Important: ``ModelRequest.runtime`` is a langgraph ``Runtime`` and does **not**
    carry ``config`` (no ``runtime.config`` field). Parent callbacks/tags live in
    the runnable contextvar instead. Falling back only to ``runtime.config`` made
    DAG subagent invokes drop LangSmith parent linkage metadata.
    """
    # 1) Ambient runnable config (callbacks / tags / metadata / configurable)
    try:
        from langchain_core.runnables.config import var_child_runnable_config

        ambient = var_child_runnable_config.get()
        if isinstance(ambient, dict) and ambient:
            return dict(ambient)
    except Exception:  # noqa: BLE001
        pass

    try:
        from langgraph.config import get_config

        ambient = get_config()
        if isinstance(ambient, dict) and ambient:
            return dict(ambient)
    except Exception:  # noqa: BLE001
        pass

    # 2) Legacy/compat: some runtime wrappers may still expose config.
    runtime = getattr(request, "runtime", None)
    config = getattr(runtime, "config", None) if runtime is not None else None
    if isinstance(config, dict) and config:
        return dict(config)
    return None


def _build_subagent_run_config(
    *,
    parent_config: dict[str, Any] | None,
    subagent_type: str,
    task_id: str,
    tool_call_id: str = "",
) -> dict[str, Any]:
    """Build child config with tracing tags + task_call namespace.

    Mirrors native deepagents task invocation enough for:
    - LangSmith subagent tree (ls_agent_type=subagent)
    - TUI nested tool attribution (task_call:{id} checkpoint_ns segment)
    """
    config: dict[str, Any] = dict(parent_config or {})
    configurable = dict(config.get("configurable") or {})

    parent_ns = str(configurable.get("checkpoint_ns") or "")
    segments: list[str] = []
    if parent_ns:
        segments.append(parent_ns)
    if tool_call_id:
        segments.append(f"task_call:{tool_call_id}")
    segments.append(f"dag_{subagent_type}_{task_id}")
    configurable["checkpoint_ns"] = "|".join(segments)
    configurable["ls_agent_type"] = "subagent"
    configurable["dag_subagent_type"] = subagent_type
    configurable["dag_task_id"] = task_id
    if tool_call_id:
        configurable["dag_tool_call_id"] = tool_call_id

    config["configurable"] = configurable

    metadata = dict(config.get("metadata") or {})
    metadata.setdefault("ls_agent_type", "subagent")
    metadata.setdefault("lc_agent_name", subagent_type)
    metadata["dag_task_id"] = task_id
    config["metadata"] = metadata

    tags = list(config.get("tags") or [])
    if "subagent" not in tags:
        tags.append("subagent")
    if subagent_type not in tags:
        tags.append(subagent_type)
    config["tags"] = tags
    config.setdefault("run_name", subagent_type)
    return config


def _append_system_message(
    existing: SystemMessage | None,
    appendix: str,
) -> SystemMessage:
    """在已有 SystemMessage 后追加内容。"""
    if existing is None:
        return SystemMessage(content=appendix)
    if isinstance(existing.content, str):
        return SystemMessage(content=existing.content + "\n\n" + appendix)
    # content_blocks 格式（Anthropic prompt caching）
    from langchain_core.messages import TextContentBlock

    blocks = list(existing.content_blocks)
    blocks.append(TextContentBlock(type="text", text="\n\n" + appendix))
    return SystemMessage(content_blocks=blocks)
