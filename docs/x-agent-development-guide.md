# x-agent 开发学习指南

以 `x-agent run "查看项目结构"` 为例，按执行顺序逐步讲解代码实现。

---

## 项目架构速览

```
x_agent/
├── main.py              # 核心：LocalKernel（JSON-RPC 服务器，composition root）
├── config.py            # 类型化分层配置（Pydantic + TOML）
│
├── agent_core/          # Agent 核心执行引擎
│   ├── graph.py         # LangGraph 包装器（AgentGraphRunner）
│   ├── agent_loop.py    # 有界循环（AgentLoop：关键词路由 + 工具调用）
│   ├── llm_client.py    # ChatModel 适配器（LangChain 协议）
│   └── prompt_templates.py  # 系统提示词
│
├── planner/             # 规划层
│   ├── strategy_router.py    # ReAct vs Plan&Execute 策略选择
│   ├── task_decomposer.py    # 任务 → DAG 分解
│   ├── plan_executor.py      # DAG 验证 + 拓扑执行
│   ├── replan_trigger.py     # 重新规划触发条件
│   └── reflector.py          # 有限纠错次数
│
├── multi_agent/         # 多 Agent 编排
│   ├── supervisor.py    # Supervisor：并行调度 Workers
│   └── worker.py        # Worker 合约（可调用封装）
│
├── harness/             # 安全护栏
│   ├── agent_harness.py # 运行生命周期（trace/audit/limits）
│   ├── permission.py    # 权限引擎（决策 + 一次性审批令牌）
│   ├── sandbox.py       # Windows 非提升沙箱（Token + Job Object）
│   ├── tracer.py        # 内存 trace 记录器
│   ├── limits.py        # 硬限制（步数/token/时间/费用）
│   ├── loop_detector.py # 循环检测
│   └── context_manager.py # 上下文管理
│
├── memory/              # 三层记忆架构
│   ├── memory_manager.py # 聚合入口（统一 API）
│   ├── working.py       # 工作记忆（持久化 KV 存储）
│   ├── short_term.py    # 短期记忆（上下文窗口 + 自动裁剪摘要）
│   └── long_term.py     # 长期记忆（向量搜索，SQLite 后端）
│
├── tools/               # 插件化工具系统
│   ├── registry.py      # 全局工具注册表（防碰撞）
│   ├── schema.py        # ToolDescriptor + JSON Schema 校验
│   ├── executor.py      # 执行链（review → execute → retry）
│   ├── filesystem.py    # 文件系统工具（沙箱化路径）
│   ├── shell_tool.py    # PowerShell 工具（沙箱运行器）
│   ├── web_search.py    # 搜索引擎边界
│   └── plugins/         # 外部插件系统
│
├── frontend/            # 三套前端
│   ├── cli/             # Click CLI（run/batch/serve 命令）
│   ├── tui/             # Textual TUI（全屏操作界面）
│   ├── acp/             # Agent Communication Protocol 桥接
│   ├── client/          # 客户端层（SSE/RPC）
│   └── protocol/        # 严格 JSON-RPC 2.0 合约
│
├── acp/                 # ACP stdio 适配器
├── rag/                 # RAG 知识管道
├── ops/                 # 运维（doctor 诊断）
└── eval/                # 评估（benchmark/LLM judge/安全测试）
```

**核心设计理念**：x-agent 采用 **Kernel + JSON-RPC 架构**。一个 `LocalKernel` 实例就是一个本地 Agent 服务器，所有交互通过 JSON-RPC 2.0 进行。

---

## 第1步：程序的"大门"——Click CLI 入口

### 1.1 frontend/cli/app.py

```python
# frontend/cli/app.py
import click

@click.group()
def main() -> None:
    """Run x-agent without starting a browser or web UI."""

@main.command()
@click.argument("input_text")
@click.option("--session-id", default="cli-session")
@click.option("--strategy", type=click.Choice(["auto", "react", "plan_execute"]), default="react")
@click.option("--permission-mode", type=click.Choice(["ask", "auto_review", "full_access"]), default="ask")
@click.option("--max-steps", type=click.IntRange(min=1), default=8)
@click.option("--max-tokens", type=click.IntRange(min=1), default=4096)
def run(input_text, session_id, strategy, permission_mode, max_steps, ...):
    ...
```

**设计要点**：使用 Click（而非 Typer），参数验证通过 `IntRange`/`Choice` 在 CLI 层完成，不等到运行时才报错。

### 1.2 `run` 命令执行流程

```python
def run(input_text, ...):
    # 1. 创建 LocalKernel 实例
    kernel = LocalKernel.from_config(config, workspace=".")

    # 2. 通过内部 asyncio 事件循环启动
    async def _execute():
        await kernel.start()
        result = await kernel.rpc(token=kernel.bootstrap.token, method="run.create", params={
            "input": input_text,
            "session_id": session_id,
            "strategy": strategy,
            "permission_mode": permission_mode,
            "limits": {"max_steps": max_steps, "max_tokens": max_tokens, ...}
        })
        return result

    result = asyncio.run(_execute())
    # 3. 打印结果
    click.echo(json.dumps(result, indent=2))
```

**设计要点**：CLI 层不直接调用 Agent 逻辑，而是通过 `kernel.rpc()` 发送 JSON-RPC 请求。这意味着 CLI 和 TUI 共享同一套后端 API。

---

## 第2步：LocalKernel —— 系统的大脑

### 2.1 什么是 LocalKernel

`main.py` 中的 `LocalKernel` 是整个系统的 **composition root**（组合根）。它负责：

1. **持有所有组件实例**（工具注册表、权限引擎、模型客户端、记忆存储、检查点等）
2. **启动 HTTP 服务器**（Starlette ASGI）
3. **JSON-RPC 分发**（将 `run.create`/`session.list` 等方法路由到处理函数）
4. **SSE 事件流**（实时推送 Agent 执行过程中的事件）

```python
class LocalKernel:
    def __init__(self, *, shell_runner, run_store, event_store, checkpointer,
                 storage_provider, chat_model):
        self.bootstrap = Bootstrap("127.0.0.1", 0, secrets.token_urlsafe(24))
        self._shell_runner = shell_runner or NonExecutingShellRunner()
        self._model_client = LangChainChatClient(chat_model) if chat_model else None
        self._knowledge = RAGPipeline()
        self._plugin_manager = PluginManager()
        self._permission_mode = PermissionMode.ASK
        # ... 更多状态

    @classmethod
    def from_config(cls, config: AgentConfig, *, workspace: str = ".") -> "LocalKernel":
        if config.shell_runner_mode is ShellRunnerMode.SANDBOX:
            return cls(shell_runner=SandboxShellRunner(workspace))
        return cls(shell_runner=NonExecutingShellRunner())
```

### 2.2 启动过程

```python
async def start(self) -> Bootstrap:
    # 动态分配端口（避免冲突）
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    self.bootstrap = Bootstrap("127.0.0.1", port, self.bootstrap.token)
    self.started = True
    return self.bootstrap
```

### 2.3 ASGI 路由

```python
def asgi_app(self) -> Starlette:
    return Starlette(routes=[
        Route("/rpc",    self._handle_rpc,    methods=["POST"]),   # JSON-RPC
        Route("/events", self._handle_events, methods=["GET"]),    # SSE 事件流
    ])
```

**两个端点**：
- `POST /rpc`：接收 JSON-RPC 请求，返回 JSON 响应（同步模式）
- `GET /events`：SSE 流式事件（实时推送 Agent 进展）

---

## 第3步：JSON-RPC 分发 —— `rpc()` 方法

### 3.1 入口

```python
async def rpc(self, *, token: str, method: str, params: dict[str, Any]) -> dict[str, Any]:
    await self.health(token=token)               # 验证令牌
    try:
        return await self._rpc_dispatch(method, params)  # 分发
    finally:
        await self._flush_event_store()          # 持久化事件
```

### 3.2 分发表（`_rpc_dispatch`）

```python
async def _rpc_dispatch(self, method: str, params: dict) -> dict:
    if method == "kernel.health":    return {"status": "ok"}
    if method == "kernel.shutdown":  return await self.shutdown(...)
    if method == "run.create":       return await self._create_run(params)
    if method == "run.get":          return await self._get_run(...)
    if method == "run.cancel":       return await self._cancel_run(...)
    if method == "permission.get":   return self._permission_status()
    if method == "permission.set":   return self._set_permission(...)
    if method == "session.list":     return await self._list_sessions()
    if method == "session.get":      return await self._get_session(...)
    if method == "checkpoint.list":  return await self._list_checkpoints(...)
    if method == "memory.search":    return await self._search_memory(...)
    # ... 共 40+ 个 RPC 方法
```

**设计要点**：这是一个 **手写分发表**，没有用反射/装饰器。每个方法名对应一个私有函数，清晰可读。"如果只加一个新功能，只需在这里加一个 `if method == ...` + 一个 `_xxx()` 方法。"

---

## 第4步：`run.create` —— 执行的核心入口

当 CLI 发送 `{"method": "run.create", "params": {"input": "查看项目结构", ...}}` 时：

### 4.1 `_create_run` 

```python
async def _create_run(self, params: dict[str, Any]) -> dict[str, Any]:
    # 1. 生成 run_id
    run_id = f"run-{len(self._events) + 1}"
    session_id = str(params.get("session_id", "session"))
    task = str(params.get("input", ""))

    # 2. 发送 "run.started" 事件
    self._append_event("run.started", run_id, session_id, {"input": task})

    # 3. 构建 AgentHarness（安全护栏）
    harness = self._build_harness(params, run_id, session_id)

    # 4. 执行
    result = await harness.run(run_id, {
        "input": task,
        "strategy": str(params.get("strategy", "react")),
        "permission_mode": str(params.get("permission_mode", "ask")),
    })

    # 5. 记录 trace
    for span in harness.trace.spans:
        self._append_event("trace.span", run_id, session_id, {...})

    # 6. 发送完成/失败事件
    self._append_event("run.completed", run_id, session_id, {"final_text": ...})
    return {"run_id": run_id, "state": result.state, "final_text": result.final_text}
```

### 4.2 `_build_harness` —— 组装安全护栏

```python
def _build_harness(self, params, run_id, session_id) -> AgentHarness:
    limits = self._limits_from_params(params)  # 从参数提取硬限制

    async def agent_node(state: dict) -> dict:
        task = str(state.get("input", ""))
        # 创建 AgentGraphRunner
        runner = AgentGraphRunner(
            max_steps=limits.steps,
            tool_executor=self._build_tool_executor(...),
            model_client=self._model_client,
        )
        # 执行 Agent
        step, final_text, error_code, agent_events = await runner.run(task)

        # 将 Agent 事件转发到事件总线
        for event in agent_events:
            self._append_event(event.type, run_id, session_id, event.data)
        return {"step": step, "final_text": final_text, "error_code": error_code}

    return AgentHarness(nodes=(agent_node,), limits=limits)
```

**设计要点**：`AgentHarness` 是 Agent 执行的"监护人"，在执行前后施加 limits/trace/audit/loop_detector。它把实际工作委托给内部的 `agent_node` 函数。

---

## 第5步：AgentGraphRunner —— LangGraph 驱动

### 5.1 graph.py

```python
class AgentGraphRunner:
    def __init__(self, *, max_steps, tool_executor, model_client):
        self.max_steps = max_steps
        self.tool_executor = tool_executor
        self.model_client = model_client
        self.graph = self._compile()

    def _compile(self):
        """构建一个最简单的 LangGraph 图：agent 节点 → END"""
        graph = StateGraph(AgentGraphState)
        graph.add_node("agent", self._agent_node)
        graph.set_entry_point("agent")
        graph.add_edge("agent", END)            # 单节点图，一次执行完成
        return graph.compile()

    async def run(self, task: str):
        output = await self.graph.ainvoke({"input": task})
        return (output["step"], output["final_text"], output["error_code"], output["events"])

    async def _agent_node(self, state):
        task = str(state.get("input", ""))
        result, events = await AgentLoop(
            max_steps=self.max_steps,
            tool_executor=self.tool_executor,
            model_client=self.model_client,
        ).run(task)
        return {"step": result.state, "final_text": result.final_text, ...}
```

**设计要点**：
- LangGraph 图非常简单：一个节点 `agent` → `END`
- 实际的循环逻辑在 `AgentLoop` 内部（不是 LangGraph 的循环边）
- 选择 LangGraph 是为了用它的 checkpointer/streaming 能力，而非复杂图结构

---

## 第6步：AgentLoop —— 核心执行循环

### 6.1 整体结构

```python
class AgentLoop:
    def __init__(self, *, max_steps=10, tool_executor=None, model_client=None):
        self.max_steps = max_steps
        self.tool_executor = tool_executor
        self.model_client = model_client

    async def run(self, task: str) -> tuple[AgentResult, list[AgentEvent]]:
        events: list[AgentEvent] = []

        # 阶段 1：检测"不可完成"任务
        if "impossible" in task.lower():
            return AgentResult("failed", error_code="UNFINISHABLE"), events

        # 阶段 2：检测 shell 命令请求（"shell:" 前缀）
        shell_command = self._requested_shell_command(task)
        if shell_command is not None:
            # → 调用 tool_executor.execute("shell", ...)
            # → 如果需要审批 → 返回 "awaiting_approval"
            ...

        # 阶段 3：检测项目结构查询
        if self._requests_project_structure(task):
            # → 调用 tool_executor.execute("builtin.filesystem.list", ...)
            # → 返回目录列表
            ...

        # 阶段 4：使用 LLM 模型回答
        if self.model_client is not None:
            answer = await self.model_client.complete(agent_messages(task))
            return AgentResult("completed", final_text=answer), events

        # 阶段 5：默认——回显输入
        return AgentResult("completed", final_text=task), events
```

### 6.2 关键词路由详解

```
用户输入 "查看项目结构"
    │
    ▼
AgentLoop.run()
    │
    ├── 包含 "impossible"?  → 返回 UNFINISHABLE
    ├── 以 "shell:" 开头?   → 执行 Shell 命令（审批→沙箱→结果）
    ├── 包含 "项目结构/目录结构/project structure"?
    │     └→ tool_executor.execute("builtin.filesystem.list", {"path": "."})
    │          └→ filesystem.list_workspace() → 返回目录摘要
    ├── model_client 存在?  → 调用 LLM 回答
    └── 否则 → 回显输入
```

**设计要点**：这是一个 **规则优先、LLM 兜底** 的架构。对于高频的简单请求（查看文件、执行命令），直接用规则匹配，避免 LLM 调用开销。复杂的开放式问题才走 LLM。

### 6.3 AgentEvent 事件类型

```python
@dataclass(frozen=True, slots=True)
class AgentEvent:
    type: str                     # 事件类型
    data: dict[str, object]       # 事件数据

# 事件类型包括：
# - "tool.requested"      模型请求调用工具
# - "tool.started"        工具开始执行
# - "tool.result"         工具执行完成
# - "assistant.delta"     模型生成增量文本
# - "approval.required"   需要用户审批
# - "reasoning.summary"   推理摘要
```

---

## 第7步：模型层 —— LLM Client

### 7.1 ChatModelClient 协议

```python
class ChatModelClient(Protocol):
    async def complete(self, messages: Sequence[BaseMessage]) -> str: ...
```

**设计要点**：用 Python Protocol（结构化鸭子类型）定义接口。任何满足 `complete(messages) -> str` 签名的对象都可以作为模型客户端。

### 7.2 LangChainChatClient 适配器

```python
class LangChainChatClient:
    def __init__(self, chat_model: BaseChatModel) -> None:
        self.chat_model = chat_model

    async def complete(self, messages: Sequence[BaseMessage]) -> str:
        response = await self.chat_model.ainvoke(list(messages))
        content = response.content
        if isinstance(content, str):
            return content
        return "".join(str(part) for part in content)
```

### 7.3 模型配置来源

```python
def default_chat_model_from_env() -> BaseChatModel | None:
    # 1. 从项目根目录的 x_agent.toml / config.toml 读取配置
    # 2. 从环境变量读取 (X_AGENT_MODEL_NAME, X_AGENT_MODEL_API_KEY, ...)
    # 3. 回退到 OPENAI_API_KEY / DEEPSEEK_API_KEY
    # 4. 使用 langchain.init_chat_model() 创建实例
```

---

## 第8步：工具系统

### 8.1 核心抽象：ToolDescriptor

```python
@dataclass(frozen=True, slots=True)
class ToolDescriptor:
    name: str                    # "builtin.filesystem.list" / "shell"
    description: str             # 用途说明
    input_schema: dict[str, Any] # JSON Schema 参数定义
    risk: str = "low"            # 风险等级：low / high
    source: str = "builtin"      # 来源：builtin / mcp / plugin
    handler: ToolHandler | None  # 实际执行函数
```

### 8.2 ToolRegistry —— 全局注册表

```python
class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, ToolDescriptor] = {}

    def register(self, descriptor: ToolDescriptor) -> None:
        if descriptor.name in self._tools:
            raise DuplicateTool(descriptor.name)    # 防碰撞
        self._tools[descriptor.name] = descriptor

    def get(self, name: str) -> ToolDescriptor:
        try:
            return self._tools[name]
        except KeyError:
            raise UnknownTool(name)                 # 未知工具异常
```

### 8.3 ToolExecutor —— 执行链

```python
class ToolExecutor:
    async def execute(self, name: str, arguments: dict) -> dict:
        descriptor = self.registry.get(name)             # ① 查找工具

        validate_arguments(descriptor.input_schema, arguments)  # ② 校验参数

        self._review_before_execution(name, arguments, descriptor.risk)  # ③ 权限审查

        try:
            result = await asyncio.wait_for(
                descriptor.handler(arguments),           # ④ 执行
                timeout=self.timeout_seconds,
            )
        except ToolExecutionError:
            raise                                          # ⑤ 错误传播
        return result
```

**执行链**：Registry 查找 → Schema 校验 → 权限审查 → 超时执行 → 结果返回

### 8.4 PermissionEngine —— 权限引擎

```python
class PermissionEngine:
    def review(self, request: ToolRequest, mode: PermissionMode) -> ReviewResult:
        if mode is PermissionMode.FULL_ACCESS:
            return ReviewResult(Decision.ALLOW, ...)

        if mode is PermissionMode.AUTO_REVIEW:
            if request.tool in self.policy["allowed_tools"] and risk == "low":
                return ReviewResult(Decision.ALLOW, ...)
            return ReviewResult(Decision.ASK, ...)

        # ASK 模式：高风险工具生成一次性审批令牌
        if request.tool == "shell":
            token = ApprovalToken(approval_id=uuid.uuid4().hex, ...)
            self._tokens[token.approval_id] = token
            return ReviewResult(Decision.ASK, ..., approval_id=token.approval_id)
```

**三种权限模式对比**：

| 模式 | shell | filesystem | 说明 |
|------|:---:|:---:|------|
| `FULL_ACCESS` | 自动通过 | 自动通过 | 无审批 |
| `AUTO_REVIEW` | 需要审批 | 自动通过 | 低风险自动，高风险审批 |
| `ASK` | 需要审批 | 自动通过 | 高风险必须审批 |

---

## 第9步：规划层 —— Strategy Routing

### 9.1 策略选择

```python
class StrategyRouter:
    def choose(self, features: TaskFeatures) -> Strategy:
        if features.step_estimate > 3 or features.has_dependencies:
            return Strategy.PLAN_EXECUTE      # 多步骤/有依赖 → 先规划再执行
        return Strategy.REACT                  # 简单任务 → ReAct 模式
```

### 9.2 TaskDecomposer —— 任务分解

```python
class TaskDecomposer:
    def decompose(self, task: str) -> Plan:
        # "step1 then step2 then step3" → DAG of 3 nodes
        parts = [part.strip() for part in task.split(" then ") if part.strip()]
        nodes = tuple(
            PlanNode(f"step-{i}", (f"step-{i-1}",) if i else (), ())
            for i, _ in enumerate(parts)
        )
        return Plan(nodes)
```

### 9.3 PlanExecutor —— DAG 验证

```python
@dataclass(frozen=True, slots=True)
class Plan:
    nodes: tuple[PlanNode, ...]

    def validate(self) -> None:
        # 检查：无重复节点、无未知依赖、无循环
        # 使用 DFS 检测环路
```

---

## 第10步：多 Agent 层 —— Supervisor-Worker

### 10.1 Worker 合约

```python
@dataclass(frozen=True, slots=True)
class Worker:
    worker_id: str
    run_callable: Callable[[], Awaitable[dict[str, Any]]]

    async def run(self) -> dict[str, Any]:
        return await self.run_callable()
```

### 10.2 Supervisor 并行调度

```python
class Supervisor:
    def __init__(self, workers: tuple[Worker, ...], *, max_parallel: int = 3):
        self.workers = workers
        self.max_parallel = max_parallel

    async def run(self) -> SupervisorResult:
        semaphore = asyncio.Semaphore(self.max_parallel)   # 控制并发数

        async def execute(worker):
            async with semaphore:
                results[worker.worker_id] = await worker.run()

        await asyncio.gather(*(execute(w) for w in self.workers))
        return SupervisorResult(results, ...)
```

**设计要点**：`asyncio.Semaphore` 控制最大并行worker数。所有 worker 并发执行，结果按 worker_id 收集。

---

## 第11步：记忆系统 —— 三层架构

```
MemoryManager (聚合入口)
├── WorkingMemory    ← 工作记忆  : 持久化 KV 存储（跨重启保留）
├── ShortTermMemory  ← 短期记忆  : 上下文窗口（自动裁剪 + 摘要）
└── LongTermMemory   ← 长期记忆  : 向量搜索（SQLite 后端）
```

### 11.1 ShortTermMemory —— 上下文窗口

```python
class ShortTermMemory:
    max_context_tokens: int = 4096

    def add(self, message: dict) -> None:
        self.messages.append(message)
        self.trim()             # 超出限制时裁剪

    def trim(self) -> None:
        total = sum(_message_tokens(item) for item in self.messages)
        if total <= self.max_context_tokens:
            return
        # 从旧到新丢弃，并生成摘要
        dropped = ...  # 被丢弃的消息
        self.summary = (self.summary + " ".join(facts)).strip()[-4000:]
        self.messages = kept

    def context(self) -> list[dict]:
        # 返回 [{"role": "summary", "content": summary}, ...messages]
```

### 11.2 LongTermMemory —— 向量搜索

```python
class LocalVectorStore:
    def __init__(self, path: Path | None = None):
        self._documents: dict[str, tuple[list[float], str, dict]] = {}

    async def search(self, query: str, *, limit: int = 10):
        query_vec = _embed(query)                    # SHA256 → 32维归一化向量
        scored = []
        for doc_id, (vec, text, meta) in self._documents.items():
            similarity = sum(a * b for a, b in zip(query_vec, vec))
            scored.append((similarity, doc_id, text, meta))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [{"id": id, "text": text, ...} for _, id, text, ... in scored[:limit]]
```

**设计要点**：向量用 SHA256 哈希伪随机生成（确定性，适用于本地小规模使用），生产环境可替换为 Chroma/Pinecone。

---

## 第12步：安全护栏 —— Harness

### 12.1 AgentHarness

```python
class AgentHarness:
    def __init__(self, *, nodes, limits, tracer, audit, loop_detector, context_manager):
        self.nodes = nodes              # 执行节点列表
        self.limits = limits            # 硬限制
        self.trace = tracer             # trace 记录器
        self.audit = audit              # 审计记录器
        self.loop_detector = loop_detector  # 循环检测
        self.context_manager = context_manager  # 上下文管理

    async def run(self, run_id, initial_state) -> RunResult:
        started = time.time()
        state = dict(initial_state or {})

        # 循环执行每个 node
        for node in self.nodes:
            state = await node(state)

        return RunResult(run_id, state["state"], state.get("final_text"), ...)
```

### 12.2 硬限制

```python
class LimitManager:
    @staticmethod
    def hard_caps(steps=10, tokens=8000, time_seconds=300, cost=0.50, ...):
        return LimitManager(
            steps=min(steps, 100),       # 最多 100 步
            tokens=min(tokens, 32000),   # 最多 32000 tokens
            time_seconds=min(time_seconds, 600),  # 最多 10 分钟
            cost=min(cost, 5.0),         # 最多 $5
        )
```

**设计要点**：`LimitManager.hard_caps()` 有不可扩展的上限。用户传的参数只能在这个范围内，防止恶意/错误的配置。

### 12.3 SandboxShellRunner —— Windows 沙箱

```python
class SandboxShellRunner:
    def run(self, command: str) -> ShellResult:
        process = self.sandbox.launch(command)   # 非提升令牌 + Job Object
        exit_code = process.wait()
        return ShellResult(command, exit_code, stdout, stderr)
```

`UnelevatedSandbox` 使用 Windows API：
- **Restricted Token**（`CreateRestrictedToken`）：移除管理员权限
- **Job Object**（`CreateJobObject`）：限制进程数
- **ACL**：限制文件系统访问路径

---

## 第13步：前端层 —— 三种交互界面

### 13.1 CLI（Click）

```python
x-agent run "查看项目结构" --strategy react --permission-mode ask --max-steps 8
```

流程：`cli/app.py:run()` → `LocalKernel.rpc(method="run.create", ...)` → 打印结果

### 13.2 TUI（Textual）

```python
x-agent tui
```

`frontend/tui/app.py` —— 全屏 TUI：
- 顶部状态栏（模型/MCP 状态）
- 转录区（user/tools/answer 时间线）
- 输入栏 + 斜杠命令补全
- 底部快捷键提示

### 13.3 ACP（Agent Communication Protocol）

`acp/stdio.py` — 外部 Agent 通过 stdin/stdout JSONL 协议通信：

```
→ {"jsonrpc":"2.0","id":"1","method":"run.create","params":{...}}
← {"jsonrpc":"2.0","id":"1","result":{...}}
```

`frontend/acp/session_bridge.py` — 将 ACP 语义翻译为内部 RPC 调用。

---

## 第14步：完整链路回顾

以 `x-agent run "查看项目结构"` 为例：

```
终端输入
  │
  ▼
frontend/cli/app.py: run(input_text="查看项目结构")
  │
  ├─ LocalKernel.from_config(config)      → 创建 kernel
  ├─ asyncio.run(kernel.start())         → 启动 HTTP server（127.0.0.1:随机端口）
  └─ kernel.rpc(method="run.create", params={
         "input": "查看项目结构",
         "session_id": "cli-session",
         "strategy": "react",
         "permission_mode": "ask",
         "limits": {"max_steps": 8, ...}
     })
       │
       ▼
  LocalKernel._rpc_dispatch()
       │
       └─ "run.create" → _create_run(params)
            │
            ├─ self._append_event("run.started", ...)
            │
            ├─ self._build_harness(params, run_id, session_id)
            │     │
            │     ├─ LimitManager.hard_caps(steps=8, tokens=4096, ...)
            │     │
            │     └─ agent_node (内部函数)
            │           │
            │           └─ AgentGraphRunner(max_steps=8, tool_executor, model_client)
            │                 │
            │                 └─ runner.run(task="查看项目结构")
            │                       │
            │                       └─ AgentLoop.run(task)
            │                             │
            │                             ├─ "impossible"? → NO
            │                             ├─ "shell:"? → NO
            │                             ├─ "项目结构" in task? → YES!
            │                             │
            │                             └─ tool_executor.execute(
            │                                   "builtin.filesystem.list",
            │                                   {"path": "."}
            │                                )
            │                                  │
            │                                  ├─ registry.get("builtin.filesystem.list")
            │                                  ├─ validate_arguments(schema, args)
            │                                  ├─ permission review (risk="low" → ALLOW)
            │                                  └─ list_workspace(".")
            │                                        └─ 返回目录摘要:
            │                                           "[D] agent_core\n[D] frontend\n..."
            │
            ├─ harness.trace.spans → 记录执行 trace
            │
            ├─ self._append_event("run.completed", ..., {"final_text": "..."})
            │
            └─ return {"run_id": "run-1", "state": "completed", "final_text": "..."}
  │
  ▼
CLI 打印结果:
  {
    "run_id": "run-1",
    "state": "completed",
    "final_text": "当前项目顶层结构:\n[D] agent_core\n[D] frontend\n..."
  }
```

---

## 与 Synapse 项目的架构对比

| 维度 | Synapse | x-agent |
|------|---------|---------|
| **核心理念** | LangChain Deep Agent（单体） | Kernel + JSON-RPC（服务化） |
| **CLI 框架** | Typer | Click |
| **Agent 图** | LangGraph 多节点（agent+tool 循环） | LangGraph 单节点（循环在 AgentLoop 内） |
| **模型客户端** | ModelRegistry + 多 profile | 简单的 ChatModelClient 协议 + 环境变量 |
| **工具系统** | @tool 装饰器 + deepagents 内置 | ToolDescriptor + ToolRegistry + Executor 链 |
| **中间件** | 5 层（error/intent/path/steer/compact） | 无中间件概念（逻辑在 harness 内） |
| **安全** | SafetyProfile（三档） | PermissionEngine（三模式 + 审批令牌） |
| **沙箱** | 无（纯 LocalShellBackend） | Windows Unelevated Sandbox |
| **记忆** | SQLite SessionStore | 三层：Working + ShortTerm + LongTerm |
| **多 Agent** | task 工具委派子 agent | Supervisor-Worker 并行模式 |
| **规划** | 无（模型自主） | StrategyRouter + TaskDecomposer + DAG |
| **前端** | CLI + TUI + Slash 命令 | CLI + TUI + ACP（3 种） |
| **协议** | 无（python 函数调用） | JSON-RPC 2.0 + SSE |
| **RAG** | 无 | 完整 RAG 管道（chunk/embed/search/rerank） |
| **MCP** | stdio/SSE/HTTP 连接池 | 同（内置在工具系统中） |
| **评估** | pytest 测试 | Benchmark + LLM Judge + 安全注入测试 |

---

## 关键设计模式总结

| 模式 | 用在哪里 | 说明 |
|------|---------|------|
| **Kernel 模式** | `LocalKernel` | 单一组合根持有所有组件 |
| **JSON-RPC 分发** | `_rpc_dispatch()` | 手写 if-else 分发，不依赖反射 |
| **Protocol 接口** | `ChatModelClient`, `ShellRunner`, `RunNode` | 结构化鸭子类型 |
| **规则优先 LLM 兜底** | `AgentLoop.run()` | 高频请求规则匹配，复杂请求走 LLM |
| **执行链** | `ToolExecutor.execute()` | 查找→校验→审查→执行→重试 |
| **Supervisor-Worker** | `multi_agent/` | 信号量控制并发 worker |
| **一次性令牌** | `ApprovalToken` + TTL | 批准后 n 秒内可执行，过期作废 |
| **分层限制** | `LimitManager.hard_caps()` | 硬上限 + 用户可调子范围 |
| **事件驱动** | `AgentEvent` + SSE | 所有执行进展通过事件流通知前端 |
| **确定性向量** | SHA256 → 32维向量 | 本地小规模使用，可替换为实际 embedding |

---

## 附录：最小化 x-agent 示例

```python
# 最精简的 x-agent 核心骨架
from x_agent.agent_core.graph import AgentGraphRunner
from x_agent.tools.registry import ToolRegistry
from x_agent.tools.executor import ToolExecutor
from x_agent.tools.schema import ToolDescriptor

# 1. 注册工具
registry = ToolRegistry()
registry.register(ToolDescriptor(
    name="hello",
    description="Say hello",
    input_schema={"type": "object", "properties": {"name": {"type": "string"}}},
    handler=lambda args: {"greeting": f"Hello, {args.get('name', 'World')}!"}
))

# 2. 创建执行器
executor = ToolExecutor(registry)

# 3. 创建 Runner
runner = AgentGraphRunner(max_steps=5, tool_executor=executor, model_client=None)

# 4. 执行
import asyncio
step, final_text, error_code, events = asyncio.run(runner.run("查看项目结构"))
print(final_text)
```
