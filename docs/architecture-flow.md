# Synapse 架构流程文档

> 基于 `src/synapse/**/*.py` 全部源文件分析，版本 v0.1.10+
> 侧重**运行时流程与组件交互**。结构总览参见 `docs/architecture.md`
> 第四轮更新：完整15中间件栈 / 工具输出变换管道 / DAG子代理调度 / MCP SessionPool / 交互账本 / Steer中程引导（2025-07）

---

## 一、启动流程：从命令行到 TUI 就绪

### 1.1 入口链

```
python -m synapse
  → __init__.py → def main() → cli_main()
  → cli.py → main() 设置 PYTHONUTF8=1 → app()
```

### 1.2 CLI 命令路由

Typer 应用树：

```
app = typer.Typer(name="synapse")
├── [默认回调] → _default_tui() → _launch_tui()       # 无子命令时启动 TUI
├── tui     → _launch_tui()                             # 显式 tui 命令
├── version → 打印版本号
├── sessions → list / codex-list / codex-inspect / codex-import
├── models  → list / current / switch
├── mcp     → list / reload
└── tool-output → stats / diagnostics
```

### 1.3 `_launch_tui()` 完整流程

```
_launch_tui()
├── _bootstrap_env()
│   ├── ensure_user_system_prompt()     # 创建 ~/.synapse/system_prompt.md
│   └── bootstrap_project_env(Path.cwd())  # 加载项目 .env (override=True)
├── _resolve_settings(workspace, model, ...)
│   └── load_settings(**overrides)
│       ├── 构造 Settings(pydantic)     # 环境变量自动注入
│       ├── 分层 settings.json 合并     # user → exe → project
│       ├── CLI overrides 覆盖
│       └── apply_models_config_to_settings()  # models.json → Settings
├── run_tui(settings, thread_id, ...)
│   ├── SessionStore → pick_startup_thread_id()
│   ├── resolve_startup_binding()       # 恢复会话级模型绑定
│   ├── bootstrap_theme()                # 加载主题
│   ├── [决策] tui_defer_agent=True（默认）
│   │   agent = None    # 延迟到后台线程构建
│   └── CodingAgentApp(agent, settings, thread_id, ...)
│       ├── compose(): 三层布局
│       │   ├── #topbar       (TopBar)
│       │   ├── #main         (WelcomeView + VerticalScroll + TurnRail)
│       │   └── #bottom-chrome (SteerQueueWidget + Input + BottomBar)
│       ├── on_mount():
│       │   ├── apply_theme()
│       │   ├── set_interval(0.1, _tick_status)
│       │   └── _bg_build_agent() [@work(thread=True)]
│       │       ├── Phase 1: build_coding_agent(load_mcp=False)
│       │       └── Phase 2: attach_mcp_to_agent()
│       └── app.run()
```

---

## 二、Agent 装配：`build_coding_agent()` 完整流程

### 2.1 装配步骤（按顺序）

```
Settings
  │
  ├─ 1. _apply_observability(settings)              ← LangSmith tracing
  ├─ 2. apply_safety_to_settings(settings, profile)  ← SafetyProfile → Settings
  ├─ 3. resolve workspace root / project_root
  ├─ 4. build_backend(settings)                      ← CodingLocalShellBackend
  ├─ 5. build_model_from_settings(settings, ...)     ← LangChain ChatModel
  │     └─ model_cache 命中(≤8个)则复用
  ├─ 6. apply_harness_exclusions(model_spec, ...)
  │     └─ 默认排除: ls, grep；readonly 额外: execute, write_file, edit_file
  ├─ 7. build_interrupt_on(require_approval=...)     ← HITL 中断配置
  ├─ 8. _build_checkpointer(settings)                ← AsyncSqliteSaver / MemorySaver
  ├─ 9. 解析 memory_paths / skills_paths
  ├─ 10. build_default_subagents(...)                ← researcher / tester / reviewer
  ├─ 11. DAGSubAgentMiddleware (可选, 并行模式)
  ├─ 12. build_filesystem_permissions(...)           ← 默认 None (LocalShellBackend)
  ├─ 13. 构造工具列表: session_tools + MCP tools + extra_tools
  ├─ 14. MCP 加载决策 (eager / deferred)
  ├─ 15. 初始化 ToolOutputRepository + ToolOutputTransformPipeline
  ├─ 16. 组装 middleware 列表 (15个中间件, 见第三节)
  ├─ 17. 可选: RAG (ProjectKnowledgeBase) / LTM (LongTermMemory)
  │
  └─ 18. create_deep_agent(
         model=model,
         system_prompt=build_system_prompt(root),
         backend=backend, tools=tools,
         middleware=middleware,       ← 15个中间件
         memory=memory_paths, skills=skills_paths,
         subagents=None,             ← DAGSubAgentMiddleware 接管
         permissions=None,           ← LocalShellBackend 不兼容
         interrupt_on=None,          ← dev-autopass 无中断
         checkpointer=saver, debug=settings.debug,
         name="coding-agent",
       )
```

### 2.2 Checkpointer 决策逻辑

```
checkpoint_backend == "memory"   → MemorySaver()
checkpoint_backend == "sqlite"   → try: AsyncSqliteSaver (WAL, 绑定 AsyncRuntime)
                                   except: sync SqliteSaver (降级)
```

### 2.3 MCP 加载时机

```
resolve_load_mcp: enable_mcp=False → 永不连接
                  load_mcp 显式传入 → 使用显式值
                  否则 → settings.mcp_eager (default False = defer)

eager  → load_mcp_server_configs() → load_mcp_tools() → 工具加载
deferred → 仅读服务器名称(UI状态栏), 后续 /mcp reload 触发 hot-attach
```

### 2.4 Agent 私有属性

| 属性 | 说明 |
|---|---|
| `_coding_model_spec` | provider:model_id 字符串 |
| `_coding_model_registry` | ModelRegistry |
| `_coding_model_cache` | dict[str, ChatModel] (≤8个) |
| `_coding_checkpointer` | AsyncSqliteSaver 实例 |
| `_coding_steer_queue` | SteerQueue (跨图重建保持) |
| `_coding_subagents` | subagent 声明列表 |
| `_coding_parallel_subagents` | DAG 并行模式是否激活 |
| `_coding_async_only` | 是否仅支持异步调用 |
| `_coding_mcp_attached` | MCP 是否已连接 |
| `_coding_knowledge_base` | ProjectKnowledgeBase (可选) |
| `_coding_long_term_memory` | LongTermMemory (可选) |

---

## 三、中间件栈（完整15中间件）

### 3.1 完整列表（按声明顺序 = 洋葱外层→内层）

| # | 中间件 | 来源 | 钩子 | 职责 |
|---|---|---|---|---|
| 1 | **AgentMdMiddleware** | `build_agent_md_middleware()` | before_model | 注入 AGENTS.md 到系统提示 |
| 2 | **DescribeImageMiddleware** | `build_describe_image_middleware()` | before_model | 图片→文本描述 |
| 3 | **NotifyingModelRetryMiddleware** | `build_model_retry_middleware()` | before_model | 瞬态错误重试 + UI 通知 |
| 4 | **TaskNamespaceMiddleware** | `build_task_namespace_middleware()` | tool_call | task 工具独立 checkpoint_ns |
| 5 | **ToolOutputTransformMiddleware** | `build_tool_output_transform_middleware()` | after_tool | 大输出→压缩+SQLite存储 |
| 6 | **ToolOutputUsageMiddleware** | `build_tool_output_usage_middleware()` | before_model | 统计 token 节省量 |
| 7 | **ToolErrorRecoveryMiddleware** | `build_tool_error_recovery_middleware()` | after_tool | 异常→ToolMessage(error) |
| 8 | **PathNormalizeMiddleware** | `build_path_normalize_middleware()` | after_tool | Windows路径→虚拟/路径 |
| 9 | **IntentSchemaMiddleware** (x2) | `build_intent_schema_middleware()` | before_model+after_tool | 注入/剥离 intent 字段 |
| 10 | **SteerMiddleware** | `build_steer_middleware(steer_queue)` | before_model | 中程用户引导注入 |
| 11 | **CompactToolMiddleware** | `build_compact_tool_middleware()` | after_tool | compact_conversation 工具 |
| 12 | **DAGSubAgentMiddleware** (可选) | `DAGSubAgentMiddleware(...)` | before_model | DAG并行子代理调度 |
| 13 | **StripRedundantPromptMiddleware** | `build_strip_redundant_prompt_blocks()` | before_model | 移除冗余 blocks |
| 14 | **CompactToolDescriptionsMiddleware** | `build_compact_tool_descriptions()` | before_model | 压缩工具描述(~30K token) |
| 15 | **ModelRequestCompressionMiddleware** | `build_model_request_compression_middleware()` | before_model | 诊断:token计数,stale替换 |

> deepagents 内置中间件(框架自动添加): SummarizationMiddleware, FilesystemMiddleware, TodoListMiddleware, SkillsMiddleware, MemoryMiddleware。DAGSubAgentMiddleware 替代内置 SubAgentMiddleware。

### 3.2 洋葱模型执行顺序

```
┌──────────────────────────────────────────────────┐
│  AgentMdMiddleware         注入 AGENTS.md        │ ← 最外层
│  ┌──────────────────────────────────────────────┐│
│  │ DescribeImageMiddleware  图片→文本            ││
│  │ ┌──────────────────────────────────────────┐ ││
│  │ │ ModelRetryMiddleware   重试 1s→8s 退避   │ ││
│  │ │ ┌──────────────────────────────────────┐ │ ││
│  │ │ │ IntentSchemaMiddleware  注入 intent  │ │ ││
│  │ │ │ ┌──────────────────────────────────┐ │ │ ││
│  │ │ │ │ SteerMiddleware  中程用户引导     │ │ │ ││
│  │ │ │ │ ┌──────────────────────────────┐ │ │ │ ││
│  │ │ │ │ │ StripRedundantMiddleware     │ │ │ │ ││
│  │ │ │ │ │ ┌──────────────────────────┐ │ │ │ │ ││
│  │ │ │ │ │ │ CompactToolDescriptions  │ │ │ │ │ ││
│  │ │ │ │ │ │ ┌──────────────────────┐ │ │ │ │ │ ││
│  │ │ │ │ │ │ │ ModelRequestCompress │ │ │ │ │ │ ││ ← 最内层
│  │ │ │ │ │ │ │ → handler(request)   │ │ │ │ │ │ ││
│  │ │ │ │ │ │ │ ← response           │ │ │ │ │ │ ││
│  │ │ │ │ │ │ └──────────────────────┘ │ │ │ │ │ ││
│  │ │ │ │ │ └──────────────────────────┘ │ │ │ │ ││
│  │ │ │ │ └──────────────────────────────┘ │ │ │ ││
│  │ │ │ └──────────────────────────────────┘ │ │ ││
│  │ │ └──────────────────────────────────────┘ │ ││
│  │ └──────────────────────────────────────────┘ ││
│  └──────────────────────────────────────────────┘│
└──────────────────────────────────────────────────┘
```

### 3.3 ModelRetryMiddleware 重试策略

- `max_retries = 999`, `initial_delay = 1.0s`, `backoff_factor = 2.0`, `max_delay = 8.0s`, `jitter = True`
- 可重试: 无状态码瞬态错误(overloaded, rate limit, empty model output, upstream timeout) + 5xx(429/502/503/504)+已知标记
- 不可重试: 任何 4xx
- 退避序列: 1s → 2s → 4s → 8s → 8s → ...
- UI 桥接: `set_retry_notifier(callback)` → TUI 状态栏更新

### 3.4 AgentMdMiddleware — AGENTS.md 注入

```
wrap_model_call: 
  读取 project_root/AGENTS.md → cwd/AGENTS.md
  注入到 system_message 第一个 text block 之后
  标记为 <project_guidelines> 块
  与 MemoryMiddleware 解耦: 总是激活, 不可写回
```

### 3.5 SteerMiddleware — 中程用户引导

```
before_model: 
  items = steer_queue.drain()  ← 线程安全取待处理引导
  if items: 注入 HumanMessage("[Mid-run user guidance]\n{guidance}")
  additional_kwargs={"coding_steer": True} → TUI 过滤不显示
```

---

## 四、运行时循环

### 4.1 Agent 图循环（单步）

```
┌─────────────────────────────────────────────────────────┐
│  1. before_model: SteerMiddleware 排空引导队列          │
│  2. wrap_model_call 洋葱层 (外层→内层)                   │
│     AgentMd → DescribeImage → Retry → Intent →          │
│     StripRedundant → CompactDesc → ModelReqCompress     │
│  3. LLM Provider 调用 → AIMessage(tool_calls=[...])     │
│  4. LangGraph ToolNode 路由                              │
│     ├─ 文件工具 → FilesystemMiddleware → backend         │
│     ├─ execute → CodingLocalShellBackend._execute()      │
│     ├─ task → DAGSubAgentMiddleware                      │
│     └─ compact_conversation → SummarizationMiddleware    │
│  5. wrap_tool_call 洋葱层                                │
│     PathNormalize → Intent(strip) → ErrorRecovery →      │
│     OutputTransform (>512B→压缩+存储)                     │
│  6. ToolOutputUsageMiddleware: 统计 token 节省           │
│  7. 回到步骤 1                                            │
└─────────────────────────────────────────────────────────┘
```

### 4.2 异步运行时 (AsyncRuntime)

进程生命周期内唯一的 asyncio 事件循环，后台 daemon 线程:

```
主线程 (TUI/CLI)                    后台线程 (coding-async-runtime)
     │                                      │
     │── get_async_runtime().run(coro) ──→  │ loop.run_forever()
     │   (阻塞等待)                          │   ├── AsyncSqliteSaver 操作
     │                                      │   ├── agent.astream/ainvoke
     │                                      │   ├── MCP session 操作
     │                                      │   └── httpx.AsyncClient
     │←──── 返回结果 ─────────────────────── │
```

---

## 五、后端执行：CodingLocalShellBackend

### 5.1 Shell 解析

```
resolve_shell_invocation(command, shell_executable):
  pwsh/powershell → [exe, -NoProfile, -NonInteractive, -Command], shell=False
  bash/sh         → [exe, -lc, command], shell=False
  cmd/system      → command, shell=True
```

### 5.2 execute() 完整流程

```
execute(command, timeout):
  1. resolve_shell_invocation → args, use_shell, executable
  2. subprocess.Popen(args, text=True, encoding="utf-8", errors="replace")
     env: PYTHONUTF8=1, PYTHONIOENCODING=utf-8
  3. proc.communicate(timeout)
  4. TimeoutExpired → _kill_process_tree (Windows: taskkill /T /F)
  5. 截断 → _max_output_bytes (默认 100KB)
  6. capture_execute_output(full, displayed, truncated) → ContextVar
  7. 返回 ExecuteResponse(output, exit_code, truncated)
```

### 5.3 安全配置 (SafetyProfile)

| 模式 | require_approval | readonly | 行为 |
|---|---|---|---|
| **dev-autopass** (默认) | False | False | 全部工具, 无HITL |
| **dev-approve** | True | False | execute/write/edit 触发HITL |
| **readonly** | False | True | 排除 write/edit/execute |

### 5.4 文件系统权限

`build_filesystem_permissions()` 在 LocalShellBackend 下默认返回 None。
原因: `FilesystemPermission` 与 execute backend 不兼容（execute 可绕过权限）。
替代: harness tool exclusion + tool exclusion middleware。

---

## 六、工具输出变换管道 (ToolOutputTransformPipeline)

### 6.1 管道总览

```
ToolOutputTransformPipeline
├── 9 种 Python Transformer (按优先级):
│   SearchTransformer → PathListTransformer → LogTransformer →
│   DiffTransformer → GitSummaryTransformer → JsonTransformer →
│   CodeTransformer → GenericTransformer
├── 叠加: 用户自定义插件 + 原生 Rust 压缩器 (NativeTransformer, 可选)
└── 内容类型检测: detect_content_type()
    优先级: SEARCH → PATHS → DIFF → GIT_SUMMARY → LOG → CODE → JSON → TEXT
```

### 6.2 transform() 执行流程

```
Phase 1: 检测 → detect_content_type(content)
Phase 2: 新鲜读取保护 → read_file+success+代码后缀+CODE → 返回原内容
Phase 3: 禁用类型检查 → content_type 在 disabled_types → 返回原内容
Phase 4: Transformer 选择 → 遍历匹配 content_type
Phase 5: 原生→Python 回退 → native unsafe → Python 重试
Phase 6: 临界行保留守卫 → critical_retained < critical_total → 拒绝
Phase 7: 字节不增长守卫 → result_bytes >= original_bytes → 拒绝
Phase 8: 通过 → 返回 TransformResult (含 stages 审计链)
```

### 6.3 ToolOutputRepository

SQLite 内容寻址存储 (6张表):

```
tool_output_blobs       ← SHA-256 主键, zlib 压缩 blob
tool_output_refs        ← ref(PK), thread_id, tool_call_id, sha256→blobs
tool_output_events      ← 变换决策事件
tool_output_retrieval_events ← 检索审计
tool_output_model_reuse_events ← 复用估算
model_request_compression_events ← 模型调用压缩记帐
interaction_events      ← 模型/工具交互事件
```

`tool-output://` 引用: `put(thread_id, content)` → SHA-256 + zlib → 生成 ref。
`get(ref, expected_thread_id)` → JOIN + 解压 + thread 鉴权。
`search(ref, query)` → token 匹配 + 错误行加权。

---

## 七、子代理系统

### 7.1 三种子代理

| 属性 | researcher | tester | reviewer |
|---|---|---|---|
| **角色** | 代码库探索者 | 测试专家 | 代码审查者 |
| **工具隔离** | 排除 write/edit/execute | 排除 write/edit | 排除 write/edit |
| **自定义 model** | researcher_model | tester_model | reviewer_model |
| **共同排除** | write_todos | write_todos | write_todos |

### 7.2 DAGSubAgentMiddleware 调度算法

```
_execute_dag(task_calls):
  1. 解析 task 调用 → tasks 列表
  2. while remaining:
       ready, remaining = _topological_waves(remaining, completed)
       if not ready and remaining → 死锁报错
       batch = ready[:max_parallel]  (默认6)
       ★ asyncio.gather(*batch) 并行执行
  3. 返回 (results, task_wave, task_deps, total_waves)
```

**depends_on 语义**: 依赖任务输出注入下游描述；循环依赖→ValueError；task() 同步阻塞: 同轮 task 在本轮内全部执行完。

---

## 八、会话管理

### 8.1 SessionStore 与 Checkpoint 的关系

```
SessionStore (sessions.sqlite)       LangGraph (checkpoints.sqlite)
┌──────────────────────┐            ┌──────────────────────────┐
│ sessions 表           │            │ checkpoints 表            │
│  - thread_id (PK) ◄──┼────────────┤  - thread_id              │
│  - title (首条消息)    │            │  - checkpoint (序列化状态) │
│  - model/active_model │            │  - parent_checkpoint_id   │
│  - thinking           │            │  - metadata               │
│  - tags/summary       │            │                           │
└──────────────────────┘            └──────────────────────────┘
```

### 8.2 ModelBinding

```
ModelBinding(active_model, model, thinking)
启动优先级: CLI --model > 会话绑定 > 全局 last-used > Settings 默认
```

### 8.3 取消后修复 (cancel_repair)

ESC 硬停后: 检测未完成 tool_calls → 生成 `ToolMessage("[cancelled by user]")` → update_state 注入 → 追加终止 AIMessage

---

## 九、工具系统

### 9.1 跨会话查阅工具

`build_session_tools()` → `[search_session, read_session, read_tool_result]`

| 工具 | 依赖 | 数据源 |
|---|---|---|
| `search_session` | SessionStore + SessionSearchIndex | sessions.sqlite + search-index.sqlite |
| `read_session` | SessionStore + SqliteSaver | sessions.sqlite + checkpoints.sqlite |
| `read_tool_result` | ToolOutputRepository | tool-outputs.sqlite |

### 9.2 read_tool_result

两种模式: 精确分页 (offset+limit, 最大500行) + 关键词召回 (query → token 匹配 + 错误行加权)。
安全: 从 ToolRuntime 获取 thread_id 鉴权，仅接受 `tool-output://` 引用。

---

## 十、配置系统

### 10.1 三级覆盖

```
Layer 1: ~/.synapse/settings.json       (user_config_dir)
Layer 2: <exe>/.synapse/settings.json   (frozen/exe only)
Layer 3: <workspace>/.synapse/settings.json (project_config_dir)
```

### 10.2 加载流程

```
load_settings(**overrides):
  1. bootstrap_project_env()      ← .env (override=True)
  2. 构造 Settings(pydantic)      ← env vars 自动注入
  3. 分层 settings.json deep_merge
  4. CLI overrides 覆盖
  5. apply_models_config_to_settings()  ← models.json → Settings
```

### 10.3 API Key 优先级

```
1. models.json profile.api_key (推荐)     ← 最高优先
2. models.json api_key_env → os.environ
3. Settings.openai_api_key / anthropic_api_key  ← .env / env
4. settings_fallback_api_key              ← 最终降级
```

### 10.4 Settings 关键字段

| 分类 | 关键字段 | 默认值 |
|---|---|---|
| 模型 | model, openai_api_key, openai_base_url, active_model | "openai:gpt-4.1" |
| 工作区 | workspace, shell_timeout(120s), max_output_bytes(100KB) | Path.cwd() |
| 安全 | require_approval(False), safety_profile("dev-autopass") | -- |
| 检查点 | checkpoint_backend("sqlite") | project/.synapse/ |
| 工具输出 | enable_tool_output_transform(True), threshold_bytes(512) | -- |
| 子代理 | enable_subagents(True), parallel_subagents(False), max_parallel(6) | -- |
| MCP | enable_mcp(True), mcp_eager(False) | -- |
| UI | theme("cursor-dark"), history_tail_turns(20) | -- |
| 记忆/RAG | enable_memory(False), enable_rag(False) | -- |

---

## 十一、集成层

### 11.1 MCP 集成

McpSessionPool: 后台 asyncio 事件循环线程 → _open_stdio/_open_http → ClientSession → list_tools → StructuredTool。错误降级: 单个服务器失败→非致命 warning，池失败→空工具列表。

### 11.2 模型管理

ModelRegistry: profiles dict + default + thinking_levels + vision_model。
ModelProfile: model, api_key/api_key_env, base_url, context_window, thinking config, extra/model_kwargs/extra_body。
模型缓存: `model_cache_key` → JSON 序列化差异 → 最多缓存 8 个 ChatModel。

### 11.3 OpenAI 兼容补丁

`llm_openai_compat.py` monkey-patch: reasoning_content delta→chunk, message→dict, dict→message 三个方向。幂等保证 (_PATCHED 标志)。

### 11.4 视觉中间件

DescribeImageMiddleware: image_input=True → 透传。否则 → VisionModelClient.describe → SHA256 缓存 → Markdown。独立视觉端点配置。

### 11.5 Codex 导入

幂等导入: CodexSessionScanner → CodexHistoryProjector → CheckpointSeeder → SessionStore → CodexImportLedger (120s lease/recover)。

### 11.6 交互账本 (InteractionLedger)

线程安全 turn/model_call 跟踪: user_fingerprint → turn_index → model_call_index → turn_id。供 ModelRequestCompressionMiddleware 诊断记帐。

---

## 十二、UI 层

### 12.1 TUI 三层布局

```
┌──────────────────────────────────────────┐
│  TopBar: path · branch · title · tokens  │
├──────────────────────────────────────────┤
│  main: WelcomeView + log + TurnRail      │
├──────────────────────────────────────────┤
│  bottom-chrome: SteerWidget + Input      │
└──────────────────────────────────────────┘
```

### 12.2 流式渲染

stream_agent → _iter_stream_events → messages(推理+token) + updates(工具结果) + heartbeat(取消检查)。TextualStreamSink: 跨线程 app.call_from_thread，自适应推送 0.12s~0.40s。

### 12.3 主题系统

内置 16 套主题。加载: ~/.synapse/themes.json → project/.synapse/themes.json → 内置默认。
切换: /theme dark → set_theme → on_theme_change → CSS 变量更新。

### 12.4 Slash 命令系统

统一 SlashResult: handled, lines, markdown, notice, exit_requested, thread_id, agent。
Tab 补全: slash_complete.py 按命令分派，支持模型名/会话ID/服务器名/主题名。

---

## 十三、关键数据流总图

```
                        ┌──────────────────────────────────────┐
                        │           CLI / TUI Entry            │
                        └──────────────────┬───────────────────┘
                                           │
                        ┌──────────────────▼───────────────────┐
                        │     settings/schema.py:load_settings │
                        └──────────────────┬───────────────────┘
                                           │
              ┌────────────────────────────┼────────────────────────────┐
              │                            │                            │
    ┌─────────▼─────────┐    ┌─────────────▼─────────────┐    ┌────────▼──────────┐
    │ config_paths.py   │    │  models/registry.py       │    │ integrations/     │
    │ layered dirs      │    │  registry_from_settings   │    │ mcp_client.py     │
    └───────────────────┘    └─────────────┬─────────────┘    └──────────────────┘
                                           │
                    ┌──────────────────────┼──────────────────────┐
                    │                      │                      │
          ┌─────────▼─────────┐  ┌────────▼────────┐  ┌──────────▼──────────┐
          │ llm_openai_compat │  │ http_clients.py │  │ llm_openai_websocket│
          │ reasoning_content │  │ AsyncClient     │  │ ResponsesWS         │
          └───────────────────┘  └─────────────────┘  └─────────────────────┘
                    │
                    ▼
          ┌─────────────────────────────────────────────────────────┐
          │              build_coding_agent()                       │
          │  Middleware: 15 个中间件 (洋葱模型)                      │
          │  Tools: session_tools + MCP tools                       │
          │  Subagents: DAGSubAgentMiddleware (researcher/tester/   │
          │             reviewer, max_parallel=6)                   │
          │  Backend: CodingLocalShellBackend (UTF-8, pwsh)         │
          │  Checkpointer: AsyncSqliteSaver (WAL)                   │
          └──────────────────────────┬──────────────────────────────┘
                                     │
                                     ▼
          ┌─────────────────────────────────────────────────────────┐
          │             create_deep_agent() → CompiledStateGraph    │
          │  内置: Summarization, Filesystem, TodoList, Skills,     │
          │        Memory middleware                                 │
          └──────────────────────────┬──────────────────────────────┘
                                     │
                                     ▼
          ┌─────────────────────────────────────────────────────────┐
          │                    运行时执行                            │
          │  agent.astream(payload, config,                          │
          │    stream_mode=["messages","updates"], version="v2")    │
          │                                                        │
          │  AsyncRuntime (后台 daemon 线程, 单事件循环):             │
          │    AsyncSqliteSaver + agent.astream + MCP sessions +     │
          │    httpx.AsyncClient                                    │
          │                                                        │
          │  持久化:                                                 │
          │    checkpoints.sqlite / sessions.sqlite /               │
          │    tool-outputs.sqlite / codex-imports.sqlite           │
          └─────────────────────────────────────────────────────────┘
```

---

## 十四、模块依赖关系图

```
agent.py ──────┬──▶ agent_md.py (AgentMdMiddleware)
               ├──▶ backends.py (CodingLocalShellBackend)
               ├──▶ safety.py (SafetyProfile → settings)
               ├──▶ harness.py (tool exclusions)
               ├──▶ middleware.py (retry, path_normalize, intent, ...)
               ├──▶ steer.py (SteerQueue → SteerMiddleware)
               ├──▶ context_compact.py (SummarizationToolMiddleware)
               ├──▶ subagents.py → parallel_subagents.py (DAG)
               ├──▶ fs_permissions.py (None for LocalShell)
               ├──▶ model_request_compression_middleware.py
               ├──▶ tool_output_middleware.py
               ├──▶ tool_output_usage_middleware.py
               └──▶ tool_output/ (pipeline, repository, transformers)

backends.py ─────┬──▶ execute_capture.py (ContextVar)
                 ├──▶ tool_ignore.py (ToolIgnoreMatcher)
                 └──▶ pathing.py (virtual path utils)

models/registry.py ──┬──▶ models/profile.py, config.py, helpers.py
                     ├──▶ integrations/llm_openai_compat.py
                     ├──▶ integrations/http_clients.py
                     └──▶ integrations/llm_openai_websocket.py

integrations/ ─────┬──▶ mcp_client.py (McpSessionPool)
                   ├──▶ codex_import.py → checkpoint_seed.py
                   ├──▶ vision_middleware.py → describe_image.py
                   └──▶ codex_history.py → codex_sessions.py

ui/ ─────────────┬──▶ tui.py (CodingAgentApp)
                 ├──▶ stream.py → stream_events.py, stream_runtime.py
                 ├──▶ timeline.py (工具分类/标签)
                 ├──▶ theme.py (16套内置主题)
                 └──▶ sink.py (StreamSink协议)

async_runtime.py ─── 独立模块 (checkpointer, stream, MCP 共用)
interaction_ledger.py ─── 独立模块 (model_request_compression 使用)
```
