# Synapse Agent 开发学习指南

以 `synapse run "查看当前仓库结构"` 为例，按执行顺序逐步讲解代码实现。

---

## 第1步：程序的"大门"——入口

### 1.1 `pyproject.toml` 声明入口点

```toml
[project.scripts]
synapse = "synapse.cli:main"
```

当你执行 `synapse run ...` 时，Python 的包管理器（`uv tool install --editable .`）会根据这行配置，自动生成一个可执行文件，调用 `synapse.cli` 模块里的 `main()` 函数。

### 1.2 `synapse/__init__.py` —— 惰性加载

```python
# src/synapse/__init__.py
def main() -> None:
    from synapse.cli import main as cli_main
    cli_main()
```

这里做了一层**惰性加载**：只有在真正调用 `main()` 时才导入 `cli` 模块。如果只是 `import synapse`（比如测试/文档工具），不会加载整个 CLI 系统。

### 1.3 `synapse.cmd` —— Windows 薄启动器

```batch
where synapse >nul 2>nul
if %ERRORLEVEL%==0 (synapse %* ; exit /b %ERRORLEVEL%)
if exist ".venv\Scripts\synapse.exe" (".venv\Scripts\synapse.exe" %* ; exit /b %ERRORLEVEL%)
if exist "uv" (uv run synapse %* ; ...)
```

**设计模式**：三层 fallback
1. 优先用 PATH 上已安装的 `synapse`
2. 其次用本地 venv 里的
3. 最后回退到 `uv run synapse`（确保 venv 依赖可用）

---

## 第2步：CLI 框架 —— Typer 命令注册

### 2.1 创建 Typer 应用

```python
# src/synapse/cli.py (第29-41行)
app = typer.Typer(name="coding-agent", no_args_is_help=True)

sessions_app = typer.Typer(help="Manage chat session metadata.")
models_app   = typer.Typer(help="List/select configured model profiles.")
mcp_app      = typer.Typer(help="Inspect MCP server configuration and tools.")

app.add_typer(sessions_app, name="sessions")
app.add_typer(models_app, name="models")
app.add_typer(mcp_app, name="mcp")
```

**设计模式**：子命令分组。`synapse sessions list` → `sessions_app` 处理，`synapse models list` → `models_app` 处理。

### 2.2 `synapse run` 命令定义

```python
@app.command("run")
def run_cmd(
    task: str = typer.Argument(...),          # 必传参数："查看当前仓库结构"
    workspace: Path | None = typer.Option(None, "--workspace", "-w"),
    model: str | None = typer.Option(None, "--model", "-m"),
    require_approval: bool = typer.Option(False, "--require-approval"),
    thread_id: str | None = typer.Option(None, "--thread-id"),
    debug: bool = typer.Option(False, "--debug"),
    stream: bool = typer.Option(True, "--stream/--no-stream"),
) -> None:
```

**关键参数**：
| 参数 | 作用 |
|------|------|
| `task` | agent 要执行的任务 |
| `-w / --workspace` | 工作目录（决定 agent 能看到哪些文件） |
| `-m / --model` | 模型 profile 名或 `provider:model` 格式 |
| `--require-approval` | 是否启用人工审批（默认关闭） |
| `--thread-id` | 指定会话 ID（恢复历史对话用） |

---

## 第3步：配置加载 —— `_bootstrap_env` + `_resolve_settings`

### 3.1 `run_cmd` 的执行起点

```python
def run_cmd(...) -> None:
    env_path = _bootstrap_env()                           # ① 加载 .env
    settings = _resolve_settings(                         # ② 合并配置
        workspace=workspace, model=model,
        require_approval=require_approval, debug=debug,
    )
    print_banner(...)                                     # ③ 打印 banner
    _print_auth_context(settings, env_path)               # ④ 打印认证信息
```

### 3.2 `_bootstrap_env()` —— 加载 .env + 初始化系统提示词

```python
def _bootstrap_env() -> Path | None:
    # 1. 尝试从 ~/.synapse/system_prompt.md 或内置默认创建系统提示词
    from synapse.prompts import ensure_user_system_prompt
    ensure_user_system_prompt()
    # 2. 搜索并加载项目目录中的 .env 文件
    return bootstrap_project_env(Path.cwd())
```

`bootstrap_project_env` 做的事情：
1. 调用 `find_dotenv()` 在项目目录及父级搜索 `.env` 文件
2. 找到后用 `load_dotenv(override=True)` 加载，**覆盖**系统环境变量
3. 这样项目的 `OPENAI_API_KEY` 优先于系统的

### 3.3 `_resolve_settings()` —— 命令行参数覆盖默认配置

```python
def _resolve_settings(*, workspace, model, require_approval, debug, readonly=None):
    overrides = {"debug": debug}
    if workspace is not None:   overrides["workspace"] = workspace
    if model is not None:       overrides["model"] = model
    if require_approval is not None: overrides["require_approval"] = require_approval
    return load_settings(**overrides)
```

`load_settings()` 是 `pydantic-settings` 的 `Settings` 类，它会：
1. 读命令行参数（最高优先级）
2. 读 `.env` 文件
3. 读系统环境变量
4. 用默认值填充

最终得到一个 `Settings` 对象，包含所有运行时参数。

### 3.4 `Settings` 类主要字段

```python
class Settings(BaseSettings):
    model: str = "openai:gpt-4.1"             # 模型 id
    openai_api_key: str | None = None          # OpenAI API key
    openai_base_url: str | None = None         # 自定义 API 端点
    workspace: Path = Path.cwd()               # 工作目录
    require_approval: bool = False             # 人工审批开关
    checkpoint_backend: str = "sqlite"         # 会话持久化后端
    enable_mcp: bool = True                    # MCP 开关
    token_stream: bool = True                  # 流式输出开关
    max_concurrency: int = 8                   # 最大并发工具调用
    theme: str = "cursor-dark"                 # TUI 主题
    # ... 还有 30+ 其他字段
```

---

## 第4步：构建 Agent —— `build_coding_agent()` 核心

这是整个系统的"心脏"。`run_cmd` 中调用：

```python
agent = build_coding_agent(settings, project_root=Path.cwd(), load_mcp=bool(settings.enable_mcp))
```

### 4.1 构建流程逐层拆解

```
build_coding_agent(settings, project_root, load_mcp)
│
├─ ① _apply_observability(settings)
│     └─ 可选的 LangSmith 链路追踪（设置环境变量 + 客户端超时 patch）
│
├─ ② apply_safety_to_settings(settings, safety_profile="dev-autopass")
│     └─ 根据安全配置写入 require_approval / readonly
│
├─ ③ build_backend(settings)
│     └─ 创建 CodingLocalShellBackend 实例
│        这是 deepagents 的 LocalShellBackend 子类
│        重写了 execute()，支持 UTF-8 编码 + 自定义 shell（pwsh/cmd/bash）
│
├─ ④ build_model_from_settings(settings)
│     ├─ registry_from_settings() → 加载 models.json → ModelRegistry
│     └─ registry.build_chat_model(name) → ChatOpenAI/ChatAnthropic 实例
│
├─ ⑤ apply_harness_exclusions(model_spec, readonly, excluded_tools)
│     └─ readonly=True 时排除 {execute, write_file, edit_file}
│
├─ ⑥ build_interrupt_on(require_approval=False)
│     └─ 决定哪些工具调用需要人工审批（默认不需要）
│
├─ ⑦ _build_checkpointer(settings)
│     ├─ "sqlite" → AsyncSqliteSaver (WAL 模式)
│     └─ "memory" → MemorySaver (无持久化)
│
├─ ⑧ build_default_subagents(enabled=True)
│     └─ 定义 2 个子 agent：tester（测试）、reviewer（代码审查）
│        通过 task 工具（deepagents 内置）委派任务
│
├─ ⑨ 工具组装
│     tools = [git_status, git_diff, run_tests,             # 内置工具
│              ...session_tools,                            # 会话工具
│              ...extra_tools,                              # 外部注入
│              ...mcp_tools]                                # MCP 工具（可选）
│
├─ ⑩ 中间件组装
│     middleware = [
│         build_tool_error_recovery_middleware(),           # 错误恢复
│         build_path_normalize_middleware(root),            # 路径标准化
│         *build_intent_schema_middleware(),                # intent 注入
│         build_steer_middleware(steer_queue),              # 中运行引导
│         build_compact_tool_middleware(model, backend),    # 上下文压缩
│     ]
│
└─ ⑪ create_deep_agent(...)
      └─ deepagents 框架函数，构建 LangGraph 图
         传入 model, system_prompt, backend, tools, middleware,
               memory, skills, subagents, checkpointer, ...
```

### 4.2 `create_deep_agent` 做了什么（框架层）

```python
from deepagents import create_deep_agent

agent = create_deep_agent(
    model=model,                        # ChatModel 实例
    system_prompt=build_system_prompt(root),  # 系统提示词
    backend=backend,                    # 文件系统 + shell 后端
    tools=tools,                        # 工具列表
    middleware=middleware,              # 中间件列表
    memory=memory_paths,               # AGENTS.md 等记忆文件
    skills=skills_paths,               # skills/ 目录
    subagents=subagents,               # 子 agent 定义
    checkpointer=saver,                # 状态持久化
    interrupt_on=interrupt_on,         # HITL 中断配置
)
```

`create_deep_agent` 内部：
1. 构建 **LangGraph StateGraph**（状态图）
2. 注册 **agent 节点**（调用 LLM + 解析工具调用）
3. 注册 **tools 节点**（执行工具调用）
4. 将 `tools` 注册为可调用工具
5. 将 `middleware` 注入到图和工具调用链中
6. 将 `subagents` 注册为内置的 `task` 工具选项
7. 配置 `checkpointer` 用于多轮对话状态保存

返回的是一个 LangGraph `CompiledGraph`，可以调用 `.astream()` / `.invoke()`。

---

## 第5步：模型注册 —— `ModelRegistry`

### 5.1 数据模型

```python
@dataclass(frozen=True)
class ModelProfile:
    name: str                    # 别名，如 "primary"
    model: str                   # provider:model，如 "openai:deepseek-v4-pro"
    api_key: str | None          # 直接写密钥（推荐）
    api_key_env: str | None      # 或从环境变量读密钥
    base_url: str | None         # 自定义 API 端点
    context_window: int | None   # 模型上下文大小（token 数）
    reasoning_effort: str | None # 思考级别
    temperature: float | None    # 温度参数
    max_tokens: int | None       # 最大输出 token
    extra: dict                  # 其他 init_chat_model 参数
```

### 5.2 配置来源（分层合并）

```
~/.synapse/models.json          ← 用户全局默认
    │
<workspace>/.synapse/models.json ← 项目覆盖（合并）
    │
环境变量 MODELS_JSON             ← 内联 JSON（调试用）
```

### 5.3 如何构建 ChatModel

```python
def build_model_from_settings(settings):
    registry = registry_from_settings(settings)   # 解析 models.json
    model = registry.build_chat_model(name)       # 创建 ChatModel 实例
    return registry, model
```

`build_chat_model` 的核心逻辑：
```python
def build_chat_model(self, name):
    profile = self.get(name)
    api_key = profile.resolved_api_key()    # 解析 ${ENV} 引用

    # 1. 启动 DeepSeek reasoning_content 补丁
    enable_openai_compat_reasoning_patch()

    # 2. 根据 provider 选择类
    if profile.model.startswith("openai:"):
        model = ChatOpenAI(
            model=profile.model.split(":", 1)[1],
            api_key=api_key,
            base_url=profile.base_url,
            reasoning_effort=profile.reasoning_effort,
            ...
        )
    elif profile.model.startswith("anthropic:"):
        model = ChatAnthropic(...)

    # 3. 写入 context_window 到 model.profile（供自动压缩用）
    apply_context_window_to_model(model, profile.context_window)

    return model
```

**设计要点**：`ModelProfile` 只是配置数据的"搬运工"，真正创建模型实例的是 `init_chat_model`（langchain 工厂函数）。

---

## 第6步：工具系统

### 6.1 自定义工具的编写模式

以 `git_status` 为例（`src/synapse/tools/git.py`）：

```python
@tool
def git_status(workspace: str = ".") -> str:
    """查看 git 工作区状态（短格式 + 分支）。

    Args:
        workspace: 仓库根目录。默认为当前工作区。
    """
    root = str(Path(workspace).expanduser().resolve())
    branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=root)
    status = _run_git(["status", "--short", "--branch"], cwd=root)
    return f"branch: {branch}\n{status}"
```

**关键点**：
- 使用 `@tool` 装饰器，LangChain 自动将函数签名转为 tool schema
- 文档字符串会被解析为 tool description（模型用来判断何时调用）
- 参数都带类型注解，框架自动生成 JSON Schema

### 6.2 工具分类

| 来源 | 工具 | 如何注册 |
|------|------|---------|
| `deepagents` 框架内置（当前保留） | `read_file`, `write_file`, `edit_file`, `execute`, `write_todos`, `task` | `create_deep_agent` 自动注入；文件读写由 Synapse backend 接管 |
| Synapse 文件工具 | `find_files`, `search_files`, `patch` | 传入 `tools=[...]`；底层调用 `synapse-core-tool` |
| 会话工具 | `list_sessions`, `read_session` | 通过 `build_session_tools()` 动态创建 |
| MCP 工具 | 由外部 MCP Server 提供 | `load_mcp_tools()` 动态加载 |

DeepAgents 的 `ls`、`glob`、`grep` 会从模型请求中隐藏，避免与 Synapse 的
`find_files`、`search_files` 产生重名语义冲突。最终系统提示词也会注入与实际 schema
一致的权威工具说明。

### 6.3 `run_tests` 工具

```python
@tool
def run_tests(workspace: str = ".", target: str = "", extra_args: str = "") -> str:
    """运行项目 pytest 测试（可指定子集）。"""
    # 优先使用 uv run pytest，没有 uv 则回退 python -m pytest
    args = ["uv", "run", "pytest"] if _has_command("uv") else ["python", "-m", "pytest"]
    # subprocess.run 执行
    # 输出截断到 20000 字符
```

---

## 第7步：中间件栈

中间件是 LangChain 的**拦截器模式**，在 Agent 图的模型调用或工具调用前后插入自定义逻辑。

### 7.1 中间件在 Agent 中的位置

```
用户消息 → [中间件.wrap_model_call] → LLM 调用 → 工具调用决策
                ↑ 注入 prompt / 引导
                              ↓
                         [中间件.wrap_tool_call] → 工具执行 → 结果返回 LLM
                              ↑ 修改参数 / 错误恢复
```

### 7.2 五个中间件逐项解析

#### (1) `tool_error_recovery` —— 错误恢复

```python
def build_tool_error_recovery_middleware():
    def wrap_tool_call(self, request, handler):
        try:
            return handler(request)         # 正常执行
        except Exception as exc:
            return ToolMessage(             # 出错时返回 ToolMessage
                content=f"Error: {name} failed: {exc}\n..."
                status="error"
            )
```

**设计要点**：工具执行出错不会炸掉整个 Agent 图。错误以 `ToolMessage(status="error")` 返回给模型，模型可以自行修正参数重试。

#### (2) `path_normalize` —— 路径标准化

```python
def build_path_normalize_middleware(workspace: Path):
    def _apply(request):
        args = request.tool_call.get("args", {})
        new_args = rewrite_tool_args_paths(args, root)  # 转换路径
        return request.override(tool_call={**tool_call, "args": new_args})
```

**设计要点**：Agent 看到的文件路径是虚拟 `/` 路径，但在 Windows 宿主上实际是 `E:\LLM学习\...`。这个中间件在工具调用前自动转换。

#### (3) `intent_schema` —— intent 字段注入

```python
TOOL_INTENT_KEY = "intent"
TOOL_INTENT_DESCRIPTION = (
    "Required. One short sentence describing WHY this tool is being called..."
)
```

**两个子中间件**：
- `wrap_model_call`：给每个工具的 `args_schema` 注入必需的 `intent: str` 字段，**强制模型声明调用意图**
- `wrap_tool_call`：执行前摘除 `intent` 字段（工具函数不需要这个参数）

#### (4) `steer_middleware` —— 中运行引导

```python
steer_queue = SteerQueue()   # 线程安全的 FIFO 队列
middleware.append(build_steer_middleware(steer_queue))
```

在每次模型调用前，检查队列是否有用户发出的引导指令。如果有，注入为 `HumanMessage("[Mid-run user guidance] ...")`。

**使用场景**：TUI 中用户在 Agent 执行期间输入 "/model thinking high" 或纠正方向。

#### (5) `compact_tool` —— 上下文压缩

```python
middleware.append(build_compact_tool_middleware(model, backend))
```

注册 `compact_conversation` 工具，模型可以在上下文过长时主动调用它来压缩对话历史。`deepagents` 框架也内置自动触发逻辑。

---

## 第8步：执行 —— `_run_once` + `stream_agent`

### 8.1 `_run_once` 函数

```python
def _run_once(agent, payload, config, *, use_stream=True, ...):
    if use_stream:
        streamed = stream_agent(agent, payload, config, ...)  # 流式执行
        return streamed.final_text, streamed.streamed_answer, streamed
    else:
        invoked = agent.invoke(payload, config=config)        # 非流式
        return extract_last_ai_text(state), False, None
```

### 8.2 `stream_agent` —— 流式事件处理核心

`stream_agent` 是连接 Agent 图和 UI 的桥梁，核心循环：

```python
def stream_agent(agent, payload, config, *, sink=None, ...):
    sink = sink or RichStreamSink()         # 默认 CLI 渲染器
    sink.activity_start("thinking", "waiting for model")

    # 核心循环：逐事件处理
    for mode, chunk, ns in _iter_stream_events(agent, payload, config, ...):
        if mode == "messages":
            # 处理文本 token、reasoning token、tool_call_chunks
            reasoning_delta = _extract_reasoning(msg_chunk)
            if reasoning_delta:
                sink.write_reasoning(reasoning_delta)

            text = _chunk_text(msg_chunk)
            if text:
                sink.write_answer_token(text, msg_id=msg_id)

            tool_call_chunks = msg_chunk.tool_call_chunks
            if tool_call_chunks:
                sink.activity_update("tool", "model requested tool call(s)")

        elif mode == "updates":
            # 处理工具调用结果
            # 处理子 agent 消息
            # ...
```

**事件类型**：
| mode | 含义 | 触发时机 |
|------|------|---------|
| `messages` | 流式消息块 | LLM 生成 token 时 |
| `updates` | 状态更新 | 工具调用完成、节点完成时 |
| `__heartbeat__` | 心跳 | 工具长时间运行时（更新 UI 状态）|
| `__cancelled__` | 取消 | 用户中断（Ctrl+C）|

### 8.3 `_iter_stream_events` —— 选择同步/异步

```python
def _iter_stream_events(agent, payload, config, *, prefer_async=True, ...):
    if prefer_async and supports_async:
        # 使用 agent.astream_events() —— asyncio 异步流
        async for event in agent.astream_events(payload, config, ...):
            yield parse_event(event)
    else:
        # 使用 agent.stream_events() —— 同步流
        for event in agent.stream_events(payload, config, ...):
            yield parse_event(event)
```

**设计要点**：当使用 `AsyncSqliteSaver` 时，必须在同一个 asyncio 事件循环上操作。项目通过 `async_runtime.py` 维护一个进程级事件循环。

---

## 第9步：UI 渲染 —— `StreamSink` 协议

### 9.1 设计模式：策略模式

```python
@runtime_checkable
class StreamSink(Protocol):
    """消费者接口 —— CLI 和 TUI 各自实现"""
    def write_reasoning(self, text: str) -> None: ...
    def write_answer_token(self, text: str, *, msg_id: str | None = None) -> None: ...
    def write_answer_complete(self, text: str, *, msg_id: str | None = None) -> None: ...
    def tool_item_started(self, id: str, name: str, ...) -> None: ...
    def tool_item_finished(self, id: str, status: str, ...) -> None: ...
```

**两个实现**：

```
StreamSink (Protocol)
├── RichStreamSink     → CLI 模式 → Rich 库终端渲染
└── TextualStreamSink  → TUI 模式 → Textual 全屏 UI
```

### 9.2 `RichStreamSink` —— CLI 渲染

```python
class RichStreamSink:
    """使用 Rich 库进行终端美化"""
    def write_reasoning(self, text):
        # 灰色/斜体显示思考过程
        self.reasoning_buf.append(text)
        self._live.update(render_reasoning())

    def write_answer_token(self, text, msg_id=None):
        # Markdown 渲染 + 流式更新
        self.answer_buf.append(text)
        self._live.update(Markdown("".join(self.answer_buf)))

    def tool_item_started(self, id, name, args_preview, ...):
        # 显示工具名 + 参数预览 + 转圈 spinner
```

### 9.3 `TextualStreamSink` —— TUI 渲染

TUI 模式下，`stream_agent` 运行在后台线程，通过 `TextualStreamSink` 将事件转为 Textual 消息，更新 App 的 widget 树。

---

## 第10步：完整链路回顾

以 `synapse run "查看当前仓库结构" -w .` 为例，从键入命令到看到结果：

```
终端输入
  │
  ▼
synapse.cmd → synapse.cli:main() → run_cmd()
  │
  ├─ _bootstrap_env()            # 加载 .env + 初始化 system_prompt.md
  ├─ _resolve_settings()         # 合并命令行参数 + 配置 → Settings 对象
  └─ build_coding_agent(settings)
       │
       ├─ 创建 CodingLocalShellBackend   # 本地 shell 后端
       ├─ 创建 ChatModel                  # 从 models.json 构建
       ├─ 组装工具 [git_status, git_diff, run_tests, ...]
       ├─ 组装中间件 [error_recovery, path_normalize, intent, steer, compact]
       ├─ 创建 AsyncSqliteSaver            # SQLite checkpoint 持久化
       └─ create_deep_agent(...)          # 构建 LangGraph 图
            │
            ▼
         返回 agent (CompiledGraph)
  │
  ├─ store.touch(tid, title_hint="查看当前仓库结构")
  │       # 写入会话元数据到 sessions.sqlite
  │
  └─ _run_once(agent, payload, config)
       │
       └─ stream_agent(agent, payload, config, sink=RichStreamSink())
            │
            ├─ agent.astream_events()     # LangGraph 异步事件流
            │     │
            │     ├─ LLM 推理 → reasoning token → sink.write_reasoning()
            │     ├─ LLM 决定调用 ls / glob → tool_call_chunks
            │     ├─ 中间件拦截：注入 intent，标准化路径
            │     ├─ 工具执行 → 结果 → 回传给 LLM
            │     └─ LLM 生成最终回答 → answer token → sink.write_answer_token()
            │
            └─ RichStreamSink 渲染到终端
                 ├─ 灰色思考链
                 ├─ 工具调用指示器（名称 + 参数 + spinner）
                 └─ Markdown 格式的最终答案
```

---

## 附录：关键设计模式一览

| 模式 | 用在哪里 | 说明 |
|------|---------|------|
| **惰性加载** | `__init__.py` → `main()` | 延迟导入 CLI，加快包导入 |
| **命令模式** | Typer `@app.command()` | 每个子命令是一个独立函数 |
| **策略模式** | `StreamSink` → `RichStreamSink` / `TextualStreamSink` | CLI/TUI 共享同一套流式事件 |
| **中间件/拦截器** | `AgentMiddleware` | 在 Agent 图的关键节点插入逻辑 |
| **依赖注入** | `build_coding_agent` 参数 | 允许外部注入 checkpointer/model/tools |
| **工厂模式** | `registry_from_settings` / `build_backend` | 根据配置创建不同实现 |
| **分层配置** | `~/.synapse/` → `<project>/.synapse/` | 用户全局 + 项目覆盖 |
| **观察者/事件流** | `stream_agent` + `StreamSink` | 流式事件驱动 UI 更新 |
| **幂等操作** | `SessionStore.ensure()` | 重复创建同一 thread_id 不会报错 |

---

## 附录：如果想自己写一个 Agent

从 Synapse 项目中可以提炼出最小化的 Agent 构建模式：

```python
# 1. 选模型
from langchain.chat_models import init_chat_model
model = init_chat_model("openai:gpt-4.1")

# 2. 准备工具
from langchain_core.tools import tool
@tool
def my_tool(query: str) -> str: ...

# 3. 构建 Agent（使用 deepagents）
from deepagents import create_deep_agent
from deepagents.backends import LocalShellBackend

agent = create_deep_agent(
    model=model,
    system_prompt="You are a helpful assistant.",
    backend=LocalShellBackend(),
    tools=[my_tool],
)

# 4. 执行
result = agent.invoke({"messages": [{"role": "user", "content": "..."}]})
```

这就是 Synapse 的核心骨架——在它的基础上，Synapse 增加了：SQLite 持久化、TUI 界面、MCP 集成、分层配置、子 Agent、中运行引导、安全审批等工程化能力。