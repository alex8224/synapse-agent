# Synapse 架构文档

> 基于 `src/synapse/**/*.py` 全部源文件分析，版本 v0.1.5

---

## 整体架构

```
                        ┌──────────────────────┐
                        │  用户 (终端/IDE)      │
                        └──────────┬───────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────┐
│                         入口层 (CLI)                                 │
│                                                                     │
│  synapse = "synapse.cli:main"          __main__.py                  │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │  Typer app (cli.py)                                         │  │
│  │  ├─ _bootstrap_env()    加载 .env + system_prompt.md         │  │
│  │  ├─ _resolve_settings() CLI参数 → Settings (Pydantic)        │  │
│  │  └─ run_tui(settings)   → 启动 Textual 全屏 TUI             │  │
│  └──────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────┬───────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      配置层 (Config)                                 │
│                                                                     │
│  config.py + config_paths.py                                       │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │  3 层覆盖模型:                                                │  │
│  │  ~/.synapse/  →  <exe>/.synapse/  →  <workspace>/.synapse/  │  │
│  │  ┌──────────┬──────────┬──────────┬──────────┬───────────┐  │  │
│  │  │models.json│mcp.json │settings  │themes.json│system_    │  │  │
│  │  │(密钥推荐) │(MCP定义)│.json     │(主题)    │prompt.md  │  │  │
│  │  └──────────┴──────────┴──────────┴──────────┴───────────┘  │  │
│  └──────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────┬───────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      TUI 层 (Textual)                                │
│                                                                     │
│  CodingAgentApp(App)  ← tui.py                                      │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │  Screen 布局                                                  │  │
│  │  ┌─────────────────────────────────────────────────────────┐ │  │
│  │  │  TopBar (高度1)                                          │ │  │
│  │  │  ├─ workspace  ≡ /project    ├─ title  会话标题          │ │  │
│  │  │  ├─ branch     ⎇ main *      └─ usage   12K/3K tokens   │ │  │
│  │  │  └─ GitChangesPopover (悬停: 脏文件列表)                  │ │  │
│  │  ├─────────────────────────────────────────────────────────┤ │  │
│  │  │  #main (高度1fr)                                         │ │  │
│  │  │  ├─ WelcomeView      Braille "SYNAPSE" 动画              │ │  │
│  │  │  ├─ #log (VerticalScroll)  ← 主时间线                    │ │  │
│  │  │  │   ├─ UserTurnBlock     ● 用户输入                     │ │  │
│  │  │  │   ├─ ThoughtBlock      ◆ 推理/思考 (可折叠)           │ │  │
│  │  │  │   ├─ ToolGroupBlock    ▾ 工具调用组 (按类型聚合)      │ │  │
│  │  │  │   ├─ AnswerDivider     ◇ 分隔线                       │ │  │
│  │  │  │   ├─ AnswerBlock       流式 Markdown 渲染             │ │  │
│  │  │  │   └─ TodoChecklist     写计划 checklist               │ │  │
│  │  │  └─ TurnRail           右侧浮动导航 (turn 刻度)          │ │  │
│  │  ├─────────────────────────────────────────────────────────┤ │  │
│  │  │  #bottom-chrome (底部固定)                                │ │  │
│  │  │  ├─ SteerQueueWidget   用户引导队列                       │ │  │
│  │  │  ├─ #status            活动指示 (busy/idle)               │ │  │
│  │  │  ├─ #complete-hint     Tab 补全提示                       │ │  │
│  │  │  ├─ #prompt (Input)    主输入框 (高度3)                   │ │  │
│  │  │  └─ BottomBar          模型 | MCP | 模式 | 快捷键        │ │  │
│  │  └─────────────────────────────────────────────────────────┘ │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                                                                     │
│  Dialog 层 (ModalScreen)                                            │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │  F2→ModelPicker   F3→ThemePicker   F4→SessionList            │  │
│  │  F5→McpPanel      F6→SafetyPanel    F7→CodexImport           │  │
│  │  F8→ThemeDesigner  TopBar→GitExploreScreen (双面板diff)      │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                                                                     │
│  流式桥梁 TextualStreamSink                                         │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │  Agent 线程 ──call_from_thread()──→ UI 线程                  │  │
│  │  stream_agent() → sink.write_reasoning/answer/tool_item()    │  │
│  │  自适应节流: 0.12s~0.40s 动态间隔                             │  │
│  └──────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────┬───────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      Agent 核心 (Deep Agents)                        │
│                                                                     │
│  build_coding_agent()  ← agent.py (一站式装配工厂)                  │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │                                                               │  │
│  │  中间件栈 (按顺序注入)                                         │  │
│  │  ┌─────────────────────────────────────────────────────────┐ │  │
│  │  │ 1. DescribeImageMiddleware    图片→文字 (视觉模型)       │ │  │
│  │  │ 2. ModelRetryMiddleware       重试 (429/5xx, 指数退避)  │ │  │
│  │  │ 3. ToolErrorRecovery          工具异常→ToolMessage       │ │  │
│  │  │ 4. TaskNamespace              子图隔离 (checkpoint_ns)   │ │  │
│  │  │ 5. PathNormalize              主机路径→虚拟/路径         │ │  │
│  │  │ 6. IntentSchema              工具注入 intent 必填字段   │ │  │
│  │  │ 7. SteerMiddleware            中程用户引导注入           │ │  │
│  │  │ 8. CompactTool                compact_conversation 工具  │ │  │
│  │  │ 9. DAGSubAgentMiddleware      DAG 并行子Agent 调度      │ │  │
│  │  └─────────────────────────────────────────────────────────┘ │  │
│  │                                                               │  │
│  │  create_deep_agent()                                          │  │
│  │  ├─ model:        BaseChatModel (来自 models_registry)       │  │
│  │  ├─ backend:      CodingLocalShellBackend                     │  │
│  │  ├─ tools:        [session_tools] + [MCP tools]               │  │
│  │  ├─ subagents:    None (由 DAGSubAgentMiddleware 接管)        │  │
│  │  ├─ checkpointer: AsyncSqliteSaver / MemorySaver              │  │
│  │  ├─ interrupt_on: {execute,write_file,edit_file} (HITL)       │  │
│  │  └─ permissions:  FilesystemPermission (非shell模式)          │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                                                                     │
│  ┌────────────────────  子Agent 系统  ──────────────────────────┐  │
│  │                                                               │  │
│  │  subagents.py          parallel_subagents.py                  │  │
│  │  build_default_subagents() → 3 种规范:                        │  │
│  │  ┌──────────┬──────────┬──────────┐                          │  │
│  │  │researcher│  tester  │ reviewer │                          │  │
│  │  │ 只读分析  │ 运行测试 │ 代码审查 │                          │  │
│  │  │ 无execute│ execute  │ 无write  │                          │  │
│  │  └──────────┴──────────┴──────────┘                          │  │
│  │                                                               │  │
│  │  DAGSubAgentMiddleware                                        │  │
│  │  ├─ _execute_dag()  解析 task() tool_calls                    │  │
│  │  ├─ _topological_waves() 拓扑排序→波次分组                    │  │
│  │  ├─ asyncio.gather(*batch) 波次内并行                        │  │
│  │  └─ depends_on 声明依赖关系                                   │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                                                                     │
│  ┌────────────────────  工具系统  ──────────────────────────────┐  │
│  │  tools/session_tools.py  (工厂+闭包依赖注入)                  │  │
│  │  ├─ list_sessions(query, limit)    搜索本地会话               │  │
│  │  └─ read_session(thread_id, ...)   读取对话历史               │  │
│  │                                                               │  │
│  │  MCP 工具  (mcp_client.py)                                    │  │
│  │  ├─ stdio / SSE / HTTP 三种传输                               │  │
│  │  ├─ McpSessionPool 进程级连接池                               │  │
│  │  └─ 工具以 StructuredTool 注入 Agent                          │  │
│  │                                                               │  │
│  │  规划器  (planner/task_planner.py)                            │  │
│  │  TaskPlanner.plan(task) → TaskPlan(steps, is_complex)         │  │
│  │  ├─ 快速启发式: <6词 → 单步                                   │  │
│  │  └─ LLM分解: 复杂任务 → 子步骤列表                            │  │
│  └──────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────┬───────────────────────────────────┘
                                  │
                    ┌─────────────┼─────────────┐
                    ▼             ▼             ▼
┌──────────────────────┐ ┌──────────────┐ ┌──────────────────────┐
│   LLM / 视觉层        │ │  后端执行层   │ │  记忆/持久化层       │
│                      │ │              │ │                      │
│ models_registry.py   │ │ backends.py  │ │ sessions.py          │
│ ┌──────────────────┐ │ │ ┌──────────┐ │ │ ┌──────────────────┐ │
│ │ModelRegistry     │ │ │ │LocalShell│ │ │ │SessionStore(SQL) │ │
│ │ profiles dict    │ │ │ │Backend   │ │ │ │ CRUD + 模型绑定   │ │
│ │ + merge (用户    │ │ │ │subprocess│ │ │ └──────────────────┘ │
│ │   项目 环境变量) │ │ │ │Popen     │ │ │                      │
│ └──────┬───────────┘ │ │ │UTF-8 safe│ │ │ checkpoint_seed.py   │
│        │             │ │ │+replace  │ │ │ CheckpointSeeder     │
│ ┌──────┴───────────┐ │ │ │编码策略  │ │ │ Codex快照→LangGraph  │
│ │build_chat_model  │ │ │ └────┬─────┘ │ │ 密封到END状态        │
│ │ OpenAI/Anthropic │ │ │      │       │ │                      │
│ │ WebSocket/HTTP   │ │ │ tool_ignore │ │ codex_*.py            │
│ └──────┬───────────┘ │ │ .gitignore  │ │ Scanner→Projector     │
│        │             │ │ 过滤glob/   │ │ →ImportService       │
│ ┌──────┴───────────┐ │ │ grep结果   │ │ 幂等三态机            │
│ │http_clients      │ │ └────────────┘ │                      │
│ │ 每模型独立        │ │              │ │ memory/               │
│ │ httpx.AsyncClient│ │ cancel_repair │ │ ┌──────────────────┐ │
│ │ 长连接池 (300s)  │ │ 取消后密封    │ │ │AutoRecorder      │ │
│ └──────────────────┘ │ checkpoint   │ │ │ 3阶段过滤→提取    │ │
│                      │              │ │ │ 教训→LongTermMem  │ │
│ vision_middleware.py │ pathing.py   │ │ └──────────────────┘ │
│ DescribeImageMW      │ 虚拟路径映射  │ │ ┌──────────────────┐ │
│ 图片→VisionModel    │ 主机↔/虚拟   │ │ │LongTermMemory(SQL)│ │
│ →文字描述           │              │ │ │文本+向量BLOB      │ │
│                      │              │ │ │余弦相似度检索     │ │
│ multimodal.py        │              │ │ └──────────────────┘ │
│ ImageBank(≤8张)     │              │ │ ┌──────────────────┐ │
│ compose_user_content│              │ │ │Embedder (共享)    │ │
│ 占位符[image#N]     │              │ │ │LocalEmbedder 384d │ │
│                      │              │ │ │SimpleEmbedder回退 │ │
│ describe_image.py    │              │ │ └──────────────────┘ │
│ VisionModelClient    │              │ │                      │
│ 独立视觉端点        │              │ │ rag/knowledge_base   │
│ 带缓存(SHA256)      │              │ │ ProjectKnowledgeBase│ │
│                      │              │ │ SQLite+向量索引     │ │
│ llm_openai_compat    │              │ │                      │
│ reasoning_content    │              │ │ context_compact.py   │ │
│ 补丁 (DeepSeek etc)  │              │ │ SummarizationMW      │ │
│                      │              │ │ +手动compact工具    │ │
│ llm_openai_websocket │              │ │                      │ │
│ ResponsesWebSocket   │              │ │ session_recap.py     │ │
│ ChatOpenAI子类       │              │ │ 空闲180s→进度摘要   │ │
│ 持久WS连接          │              │ │                      │ │
└──────────────────────┘ └──────────────┘ └──────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│                   横切关注点 (Cross-cutting)                         │
│                                                                     │
│  safety.py + hitl.py + fs_permissions.py                           │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │  3 层安全边界:                                                │  │
│  │  配置层: safety_profile (dev-autopass / dev-approve / readonly)│  │
│  │  中间件层: ToolExclusionMW (hidden tools) + interrupt_on      │  │
│  │  执行层: check_command() 黑名单 + HITL interrupt/resume       │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                                                                     │
│  steer.py — 中程用户引导队列 (FIFO, 线程安全)                       │
│  slash_cmds.py — /help /model /theme /sessions /mcp /compact ...   │
│  slash_complete.py — Tab 补全 (静态+动态: 会话/模型/MCP名)         │
│  input_history.py — 输入历史 (<=1000条, 多编码兼容)                 │
│  async_runtime.py — 进程级守护事件循环 (AsyncSqliteSaver共享)       │
│  skills_catalog.py — SKILL.md 发现 + YAML 解析                      │
│  startup_trace.py — 启动耗时追踪 (AGENT_STARTUP_TRACE=1)           │
│  transcript.py — 消息加载 (checkpoint->parent链->.md回退) + 导出   │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 模块依赖关系

```
cli.py
  ├── config.py ───── config_paths.py ─── models_registry.py ─── ui/theme.py
  │                                            │
  │                              ┌─────────────┼─────────────┐
  │                              ▼             ▼             ▼
  │                    llm_openai_compat  http_clients  llm_openai_websocket
  │
  ├── prompts.py ─── skills_catalog.py
  │
  ├── ui/tui.py (CodingAgentApp)  ← 主 TUI
  │     ├── ui/stream.py          ← 流式循环 + Markdown/Mermaid 渲染
  │     ├── ui/timeline.py        ← 数据模型 (ToolItem/ToolGroup)
  │     ├── ui/sink.py            ← StreamSink 协议
  │     ├── ui/steer_widget.py    ← 引导队列面板
  │     ├── ui/topbar/            ← TopBar + GitChrome
  │     ├── ui/bottombar/         ← BottomBar
  │     ├── ui/dialogs/           ← DialogBase + 8 种 Dialog
  │     └── ui/git_explore/       ← Diff引擎 (textual_diff_view / unified)
  │
  └── agent.py (build_coding_agent)
        ├── backends.py           ← CodingLocalShellBackend
        ├── middleware.py         ← 中间件工厂
        ├── subagents.py          ← 子Agent 规范
        ├── parallel_subagents.py ← DAG 调度
        ├── mcp_client.py         ← MCP 集成
        ├── safety.py ── hitl.py ── fs_permissions.py
        ├── vision_middleware.py ── describe_image.py ── multimodal.py
        ├── context_compact.py
        ├── tools/session_tools.py ── sessions.py
        ├── memory/               ← AutoRecorder + LongTermMemory + Embedder
        ├── rag/                  ← ProjectKnowledgeBase
        ├── codex_*.py            ← Codex 发现/投影/导入
        ├── checkpoint_seed.py    ← LangGraph 种子
        ├── cancel_repair.py
        └── transcript.py
```

---

## 核心数据流（一次用户对话）

```
用户输入 (斜杠命令/普通消息)
  │
  ├─ 斜杠命令? → slash_cmds.handle_slash()
  │   /model → rebuild agent
  │   /compact → force_compact_via_agent
  │   /switch → restore thread + model
  │
  └─ 普通消息 → agent.ainvoke(config)
      │
      ▼
  ┌─ [中间件: awrap_model_call 链] ─────────────────────────────┐
      │
      ├─ vision: 图片→文字 (非视觉模型)
      ├─ retry:  429/5xx 重试
      ├─ steer:  注入用户引导
      ├─ DAG:    发现 task() 调用 → 拓扑排序 → 波次并行子Agent
      └─ compact: 自动压缩 (token阈值触发)
      │
      ▼
  ┌─ [LLM 推理] ──────────────────────────────────────────────┐
      │  models_registry → build_chat_model
      │  OpenAI (HTTP/WebSocket) / Anthropic
      ▼
  ┌─ [ToolNode: 工具执行] ────────────────────────────────────┐
      │
      ├─ read_file/write_file/edit_file/glob/grep/execute
      │    └→ CodingLocalShellBackend (subprocess)
      ├─ task (子Agent)
      │    └→ DAGSubAgentMiddleware (缓存结果)
      ├─ list_sessions/read_session
      │    └→ SessionStore (SQLite)
      ├─ MCP tools
      │    └→ McpSessionPool (stdio/SSE/HTTP)
      └─ compact_conversation
           └→ SummarizationMiddleware 触发压缩
      │
      ▼
  ┌─ [结果持久化] ────────────────────────────────────────────┐
      │
      ├─ LangGraph Checkpoint (AsyncSqliteSaver)
      ├─ SessionStore.touch() → 更新标题/时间
      ├─ AutoRecorder.record_if_valuable() → 提取教训 → LTM
      └─ SummarizationMiddleware → conversation_history/{id}.md
      │
      ▼
  ┌─ [UI 渲染] ───────────────────────────────────────────────┐
      │
      └─ TextualStreamSink (call_from_thread)
           ├─ commit_thought/answer → 时间线块
           ├─ tool_item_started/finished → ToolGroupBlock
           ├─ usage → TopBar 更新
           └─ activity → BottomBar 状态
```

---

## 持久化存储总览

| 存储 | 格式 | 内容 |
|------|------|------|
| LangGraph Checkpoint | SQLite (Saver) | 会话消息 / graph state |
| SessionStore | SQLite | 会话元数据 + 模型偏好 |
| LongTermMemory | SQLite (WAL) | 记忆文本 + 向量 BLOB |
| CodexImportLedger | SQLite | source->thread 映射 + 租约 |
| Codex state DB | SQLite (只读) | 原始 Codex thread 元数据 |
| Codex rollouts | JSONL / JSONL.zst | 原始 turn 事件流 |
| conversation_history | Markdown (.md) | 压缩后的对话摘要 |

---

## 关键设计模式

| 模式 | 应用位置 |
|------|---------|
| **工厂模式** | `build_coding_agent()`, `build_backend()`, `build_default_subagents()`, `build_session_tools()` |
| **中间件链** | `AgentMiddleware.awrap_model_call` 链式拦截 (9 个中间件) |
| **依赖注入** | `SessionStore` 闭包注入工具; 子Agent 中间件注入 Backend |
| **拓扑排序+DAG** | `DAGSubAgentMiddleware._topological_waves()` 波次并行 |
| **预编译缓存** | `compile_subagent_runnables()` 预编译子Agent图 |
| **3层覆盖配置** | 用户 `~/.synapse/` -> 便携包 -> 项目 `.synapse/` |
| **幂等三态机** | `CodexImportLedger.claim()` (new/completed/recover) |
| **自适应节流** | `_stream_interval()` 0.12s~0.40s 动态间隔 |
| **退化保护** | TaskPlanner 快速启发式; MCP 连接断开自动移除; DAG 未启用时回退原生 |

---

## 模块清单

| 模块 | 核心类/函数 | 职责 |
|------|-----------|------|
| `cli.py` | `app`, `_bootstrap_env`, `_launch_tui` | CLI 入口, Typer 组装 |
| `config.py` | `Settings`, `load_settings` | Pydantic Settings, 多层合并 |
| `config_paths.py` | `layered_config_dirs`, `deep_merge_dict` | 配置路径解析 |
| `agent.py` | `build_coding_agent` | Agent 图装配, 中间件组合 |
| `backends.py` | `CodingLocalShellBackend`, `build_backend` | 本地 Shell 执行 |
| `middleware.py` | `build_*_middleware` 工厂函数 | 中间件构造 |
| `subagents.py` | `build_default_subagents` | 3 种子Agent 规范 |
| `parallel_subagents.py` | `DAGSubAgentMiddleware` | DAG 拓扑调度 |
| `tools/session_tools.py` | `build_session_tools` | 会话查阅工具 |
| `mcp_client.py` | `McpSessionPool`, `load_mcp_tools` | MCP 协议集成 |
| `planner/task_planner.py` | `TaskPlanner`, `TaskPlan` | 任务分解规划 |
| `slash_cmds.py` | `handle_slash`, `SlashResult` | 斜杠命令路由 |
| `slash_complete.py` | `complete_slash`, `SlashCompleteContext` | Tab 补全 |
| `safety.py` | `apply_safety_to_settings`, `check_command` | 安全策略 |
| `hitl.py` | `extract_pending_interrupt`, `build_resume_payload` | HITL 审批 |
| `fs_permissions.py` | `build_filesystem_permissions` | 文件系统权限 |
| `steer.py` | `SteerQueue` | 中程用户引导 |
| `cancel_repair.py` | `repair_thread_after_cancel` | 取消修复 |
| `async_runtime.py` | `AsyncRuntime`, `get_async_runtime` | 进程级事件循环 |
| `input_history.py` | `InputHistory` | 输入历史管理 |
| `startup_trace.py` | `StartupTrace`, `TRACE` | 启动耗时追踪 |
| `prompts.py` | `load_coding_system_prompt` | System Prompt 管理 |
| `skills_catalog.py` | `discover_skills` | 技能目录扫描 |
| `models_registry.py` | `ModelRegistry`, `build_chat_model` | 模型注册与构建 |
| `llm_openai_compat.py` | `enable_openai_compat_reasoning_patch` | reasoning 补丁 |
| `llm_openai_websocket.py` | `ResponsesWebSocketChatOpenAI` | WebSocket 传输 |
| `http_clients.py` | `build_openai_async_http_client` | HTTP 客户端管理 |
| `multimodal.py` | `ImageBank`, `compose_user_content` | 多模态内容处理 |
| `describe_image.py` | `VisionModelClient` | 视觉模型调用 |
| `vision_middleware.py` | `DescribeImageMiddleware` | 图片拦截转换 |
| `pathing.py` | `to_virtual_path`, `rewrite_tool_args_paths` | 虚拟路径映射 |
| `tool_ignore.py` | `ToolIgnoreMatcher` | .gitignore 规则过滤 |
| `sessions.py` | `SessionStore`, `SessionInfo` | 会话元数据 CRUD |
| `checkpoint_seed.py` | `CheckpointSeeder` | 检查点种子写入 |
| `codex_sessions.py` | `CodexSessionScanner` | Codex 会话发现 |
| `codex_history.py` | `CodexHistoryProjector` | Codex 历史投影 |
| `codex_import.py` | `CodexImportService`, `CodexImportLedger` | Codex 导入 |
| `context_compact.py` | `force_compact_via_agent` | 上下文压缩 |
| `session_recap.py` | `SessionRecapController` | 空闲回顾 |
| `transcript.py` | `load_thread_messages`, `fold_messages_for_ui` | 转录/回放 |
| `memory/auto_recorder.py` | `AutoRecorder` | 自动提取教训 |
| `memory/long_term.py` | `LongTermMemory` | 长期记忆存储 |
| `memory/embedder.py` | `LocalEmbedder`, `SimpleEmbedder` | 向量嵌入 |
| `rag/knowledge_base.py` | `ProjectKnowledgeBase` | RAG 知识库 |
| `ui/tui.py` | `CodingAgentApp`, `TextualStreamSink` | TUI 主应用 |
| `ui/stream.py` | `stream_agent`, `render_markdown` | 流式输出 |
| `ui/timeline.py` | `ToolItem`, `ToolGroup`, `build_tool_item` | 时间线数据模型 |
| `ui/theme.py` | `Theme`, `get_theme`, `set_theme`, `list_themes` | 主题系统 |
| `ui/topbar/core.py` | `TopBarRegistry`, `layout_from_registry` | TopBar 布局引擎 |
| `ui/topbar/git_chrome.py` | `GitBranchChrome`, `probe_git_branch_chrome` | Git 状态探测 |
| `ui/bottombar/core.py` | `BottomBarRegistry` (复用 TopBar 引擎) | BottomBar 布局 |
| `ui/dialogs/base.py` | `DialogBase`, `OptionItem` | Dialog 基类 |
| `ui/git_explore/provider.py` | `DiffPayload`, `load_file_diff` | Diff 数据加载 |
| `ui/git_explore/engine.py` | `make_diff_view`, `fallback_renderable` | Diff 渲染 |

---

## 补充架构细节

### TurnRail 迷你地图

```
TurnRail (Vertical, id="turn-rail", dock:right, overlay, width:34)
  └── TurnRailItem 或 TurnRailGap × N 行
```

**映射算法** `turn_rail_tick_slots(n, height)`:
- `n <= height`: 紧凑居中对齐，每个 turn 占一行
- `n > height`: 比例桶合并 `y = i * h // n`，相邻 turn 映射到同一行

**TurnRailItem 交互**:
- 悬停 → 显示预览文本（用户输入前 28 字符）
- 点击 → `jump_to_user_turn(target)` 滚动到对应 UserTurnBlock
- 多点槽位支持 `_cycle` 循环（`(_cycle + 1) % len(targets)`）
- 密度视觉编码：单 turn `───`，2-3 turn `━━━`，4+ turn `▓▓▓`

**更新触发**: `append_user()` → `_refresh_turn_rail()` → `rail.set_turns(turns)` → `relayout()`；`on_resize()` 同样触发。

### Dialog 系统交互逻辑

所有 Dialog 继承 `DialogBase(ModalScreen)`，统一模板：66列宽 x 28行高，border round。键盘绑定：↑↓ 导航、Enter 确认、Esc 关闭。

| Dialog | 返回值 | 独特行为 |
|--------|--------|---------|
| ModelPickerDialog | `("model", alias)` 或 `("thinking", level)` | 支持别名列表 + thinking 级别 |
| ThemePickerDialog | `("theme", name)` | 内置+自定义主题，标记当前激活 |
| SessionListDialog | `("switch", tid)` 或 `("delete", tid)` | 双模式切换/删除，显示 turn 数 |
| McpPanelDialog | `("mcp-toggle", name)` 或 `("mcp-reload", None)` | `r` 键重载 MCP 配置 |
| SafetyPanelDialog | `("safety", key)` | 三选一 profile (dev-autopass/dev-approve/readonly) |
| CodexSessionListDialog | `("codex-import", native_id)` | 扫描 Codex 目录，只列可导入会话 |
| SubagentMonitorDialog | (只读) | 独占 ModalScreen，不走 DialogBase，显示 DAG 列表+详情 |
| ThemeDesignerDialog | (直接写文件) | 独占 ModalScreen，HSV 选择器+JSON 预览+实时 CSS 刷新 |

统一调用模式：`self.push_screen(SomeDialog(...), self._on_some_dialog_done)`，回调中解包 `(action, value)` 元组。

### 工具排除的两层机制

Synapse 有**两层**工具排除，分别作用于不同范围：

**第1层: HarnessProfile（deepagents 内置）**
- 通过 `register_harness_profile()` 注册到 deepagents 全局 registry
- 同时以模型名 `"openai:gpt-4.1"` 和 provider 名 `"openai"` 双重注册
- 默认排除 `{"ls", "grep"}`（用 `execute` 代替）；readonly 模式额外排除 `{"execute", "write_file", "edit_file"}`
- 只能排除 deepagents 内置工具，不能排除用户通过 `tools=` 参数传入的工具

**第2层: ToolExclusion 中间件（Synapse 自定义）**
- `build_tool_exclusion_middleware(excluded)` 在 `wrap_model_call` hook 中过滤 `ModelRequest.tools` 列表
- 作用于所有工具（内置 + 用户自定义 + MCP），更彻底
- 子 Agent 通过此中间件实现工具隔离

### AsyncRuntime 进程级单例

**设计动机**: LangGraph 的 `AsyncSqliteSaver` 绑定到创建它的事件循环，不能在多个 `asyncio.run()` 之间共享。

**实现**:
```
AsyncRuntime (进程级全局 _RUNTIME)
  └── 守护线程: threading.Thread(daemon=True, target=_thread_main)
        └── loop = asyncio.new_event_loop()
           loop.run_forever()  # 永久运行
```

**提交机制**: `runtime.run(coro)` → `asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=...)`

**两种路径差异**:
| 维度 | AsyncSqliteSaver（默认） | MemorySaver |
|------|-------------------------|-------------|
| 存储 | 持久化 SQLite | 纯内存 dict |
| 跨 turn 持久 | 是 | 否（进程重启丢失） |
| 事件循环要求 | 必须在创建它的 loop 上操作 | 无约束 |
| AsyncRuntime 依赖 | 强依赖 | 无需 |

### Embedder 工厂与退化

```
_build_default_embedder()
  ├─ try: LocalEmbedder()        → sentence-transformers 可用 → 384d (all-MiniLM-L6-v2)
  │    └─ ImportError
  └─ SimpleEmbedder()            → 零依赖回退 → 256d TF-IDF 词袋
```

**SimpleEmbedder TF-IDF 实现**: 取 TF 最高的 256 个词构建词表，IDF 权重 `log((N+1)/(df+1))+1`，L2 归一化。词表外词汇 (OOV) 静默丢弃。

**注意**: `LocalEmbedder` 的导入是**延迟的**（首次 `embed()` 时才加载 `sentence-transformers`），因此工厂函数的 `try/except ImportError` 在其构造函数中永远不会触发——这是一个已知的潜在问题。

### Reasoning Content 补丁

`llm_openai_compat.py` 对 `langchain_openai` 的三个转换函数进行 monkey-patch，以保留 DeepSeek 等模型的 `reasoning_content` 字段：

1. `_convert_delta_to_message_chunk`: 流式增量 `delta.reasoning_content` → `AIMessageChunk.additional_kwargs['reasoning_content']`
2. `_convert_dict_to_message`: 完整消息同理
3. `_convert_message_to_dict`: 发送时将 prior `reasoning_content` 回写到请求体（DeepSeek 工具多轮要求返回之前的思考内容）

### HTTP 客户端管理

`http_clients.py` 为每个模型创建独立的 `httpx.AsyncClient`，关键配置：
- `keepalive_expiry=300s`（5 分钟长连接）
- `max_connections=1000`, `max_keepalive_connections=100`
- 客户端在 AsyncRuntime 事件循环上通过 `runtime.run(_build())` 创建
- 通过 `async def _async_api_key()` callable 注入 ChatOpenAI，阻止其创建同步客户端

三个补丁函数 (`enable_openai_long_keepalive_defaults` / `enable_anthropic_long_keepalive_defaults` / `enable_long_keepalive_http_defaults`) 修改 OpenAI/Anthropic SDK 的默认连接限制，并清除 `langchain_openai` 的 httpx 客户端缓存。

---

## 第二轮补充：MCP / Session / Codex / Vision / Theme / Slash / History

### MCP 集成深层架构

**`McpSessionPool` 进程级单例**:
```
McpSessionPool (进程级 _ACTIVE_POOL，由 _POOL_LOCK 保护)
  └── _LoopThread (守护线程，独立 asyncio 事件循环)
        └── submit(coro) → asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=120)
```

**`_open_one()` 三种传输路由**:
| transport | 方法 | 客户端模块 |
|-----------|------|-----------|
| `"stdio"` | `_open_stdio()` | `mcp.client.stdio.stdio_client` → subprocess |
| `"sse"` | `_open_http()` + sse | `mcp.client.sse.sse_client` |
| `"streamable_http"` / `"http"` | `_open_http()` + http | `mcp.client.streamable_http.streamablehttp_client` |

**`_make_tool()` 转换链路（5 步）**:
1. `_json_schema_to_args()` — 规范化 JSON Schema 为 `{"type":"object","properties":{...}}`
2. `_annotation_for_prop()` — JSON Schema type → Python 类型注解（递归处理 array/anyOf/$ref）
3. `json_schema_to_pydantic_model()` — `pydantic.create_model()` 动态创建，`extra="allow"` 前向兼容
4. 闭包 `_invoke()` — 过滤显式 None 值后调用 `call_tool()`
5. `StructuredTool.from_function()` — 最终工具对象

**连接断开检测**（被动检测）：`_call()` 中任何异常 → `self._servers.pop(name, None)` 立即移除死连接 → 返回错误消息。Pool 关闭时先置空 `tools`/`tool_names` 再关连接，旧工具引用快速失败。

**环境变量替换**：`McpServerConfig` 所有字符串字段经过 `_expand_env()`：正则 `\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)` 匹配 `${VAR}` 和 `$VAR` 两种写法。

### Session 管理与模型绑定

**`sessions` 表 schema**:
| 列 | 类型 | 说明 |
|---|---|---|
| `thread_id` | TEXT PK | LangGraph 线程 ID |
| `title` | TEXT NOT NULL | 首条用户消息前 80 字符 |
| `model` | TEXT | 具体模型 ID |
| `active_model` | TEXT | 配置别名 |
| `thinking` | TEXT | off/minimal/low/medium/high/max/on |
| `tags_json` | TEXT NOT NULL DEFAULT '[]' | JSON 标签 |
| `summary` | TEXT | 会话摘要 |
| `created_at` / `updated_at` | TEXT NOT NULL | ISO 8601 UTC |

Schema 演进：`_ensure_column()` 用 `PRAGMA table_info` 检测 `active_model`/`thinking` 列，缺则 `ALTER TABLE ADD COLUMN`。

**`touch()` 标题自动绑定**：首条用户消息后，若当前标题是占位符（thread_id 自身、"session ..."等），自动替换为用户输入的前 80 字符。

**`pick_startup_thread_id()` 恢复优先级**：显式传入 → `store.latest_nonempty()` 最新非占位会话 → `uuid4().hex[:12]` 新建。新 thread_id 不立即持久化。

**`resolve_startup_binding()` 模型恢复**：CLI --model 显式 > 会话级绑定 (`get_model_binding(thread_id)`) > 全局最后使用 (`get_last_model_binding`)。通过 `apply_binding_to_settings()` 写回 settings。

**`build_session_tools()` 闭包依赖注入**：`SessionStore` 和 `checkpoint_path` 通过闭包注入两个工具（`list_sessions` / `read_session`）。两个工具标记了 `**默认禁止调用**` docstring。

### Codex 导入完整流水线

**三阶段流水线**:
```
CodexSessionScanner.inspect(native_id)          → CodexSession (元数据)
CodexHistoryProjector.project_path(rollout_path) → CodexTextSnapshot (不可变)
CodexImportService.import_snapshot(native_id, snapshot, title) → thread_id
```

**Scanner 安全约束**:
- `_ROLLOUT_RE` 正则强制文件名 UUID 格式
- `_validated_rollout_path()` 用 `resolve(strict=True)` + `_is_under()` 防止路径遍历
- 回退扫描 `_scan_rollout_headers()` 支持 zstandard 压缩，最大 32MB/文件
- `sessions_root` 是符号链接则拒绝（安全策略）

**Projector 消息提取状态机**: 只接受 `event_msg` 和 `compacted` 两类记录。显式忽略 `response_item`（避免重复 UI 文本）、`agent_reasoning`、`session_meta` 等。append_message 过滤内部标记（`<environment_context>` 等）。

**ImportService 幂等三态机**:
```
ledger.claim(source_id, digest, proposed_thread_id):
  ├─ "new"       → _seed_new() → seeder.seed_snapshot() + sessions.ensure() + ledger.complete()
  ├─ "completed" → _verify_completed()  幂等复用
  └─ "recover"   → _recover()  崩溃恢复 → 验证 → 补偿 → 重新 seed
  异常 → _compensate_new(): 回滚 SessionStore + Checkpoint + Ledger
```

**CheckpointSeeder.seed_snapshot()**: 消息写入 → `agent.update_state(config, {"messages": expected}, as_node="model")` → `agent.update_state(seeded_config, None, as_node=END)` 标记终止 → 验证 state.next 为空 + messages 逐条匹配。异常时 `_compensate()` 调用 `saver.delete_thread()`。

**CodexImportLedger 租约机制**: `BEGIN IMMEDIATE` 原子 claim，120 秒租约防止并发导入，支持崩溃后自然过期接管。

### 视觉/多模态管线

**完整链路（三种场景）**:

场景 1（原生视觉模型如 GPT-4o）: 图片 → `ImageBank` → `compose_user_content()` 生成 `image_url` blocks → DescribeImageMiddleware 透传 → LLM 收到多模态 content

场景 2（纯文本模型如 DeepSeek V3）: 图片 → ImageBank → compose → DescribeImageMiddleware 拦截 → VisionModelClient.describe_data_url() → `[image]\n...描述...\n[/image]` 文本替换 → LLM 收到纯文本

场景 3（无 Vision Model 配置）: 同上但 `VisionModelClient` 为 None → `[image unavailable: automatic description failed]`

**ImageBank 关键约束**: 容量上限 8 张，单张 4MB 限制。**每 turn 清空**（不能跨 turn 引用）。支持 4 种来源：剪贴板（PIL/PS/pngpaste/wl-paste）、文件路径、剪贴板文本→路径自动识别、已有 image_url blocks。

**`compose_user_content()` 占位符替换**: 正则 `\[image#(\d+)\]` 扫描文本 → 缺失占位符检查 → 所有 blocks 无有效文本时自动插入 `"(see attached image)"` → provider 自适应（OpenAI/Anthropic/Google 三种 block 格式）。

**`model_supports_image_input()` 判断**: 用户显式声明优先 → Anthropic Claude 3/4 系列 → Gemini 系列 → 官方 OpenAI 端点检查模型名 → 非官方端点默认 False（保守安全策略）。

**VisionModelClient**: SHA256 内存缓存、双模型 fallback（主模型失败→备用模型）、重试 2 次（仅 429/5xx/httpx 异常）、超时 45s、`allow_remote_urls` 默认 False 防 SSRF。

### Theme 系统与热切换

**双通道色彩注入**:
- **Textual CSS 变量** (`$theme-*`): `Theme.css_variables()` → `app.refresh_css()` → 所有 Widget CSS 自动更新
- **Rich 色彩槽**: 对于将颜色"烤入" Rich Text 的组件（WelcomeView Braille 动画、UserTurnBlock、ThoughtBlock 等），需手动调用 `_repaint_themed_widgets()` 重渲染

**热切换完整链路（无需重启）**: `/theme name` → `set_theme()` 更新全局 `_active` → `apply_textual_theme(app)` 切换 App.theme → `app.refresh_css()` 注入新 CSS 变量 → `_repaint_themed_widgets()` 遍历 7 类 Rich Widget 重渲染。

**16 个内置主题**: ansi（透明终端）、5 个 dark（cursor-dark/github-dark/dracula/nord/solarized-dark/catppuccin-mocha/one-dark）、7 个 light。自定义主题通过 `themes.json` 用户层→项目层叠加加载。

**ThemeDesignerDialog**: HSV 调色板（HueStrip + SV 平面），180ms debounce 实时预览。`_hsv_to_hex()` 通过 `colorsys.hsv_to_rgb()` 转换。

### Slash 命令路由表

**命令路由完整列表**（26+ 命令）:

| 类别 | 命令 | 行为 |
|------|------|------|
| **系统** | `/help`, `/?`, `/exit`, `/quit`, `/clear`, `/thread`, `/id` | 帮助/退出/清屏/线程ID |
| **会话** | `/sessions [list/search]`, `/session [list/show/new/switch/rename/delete/search/export]`, `/new`, `/switch`, `/rename`, `/export` | 会话 CRUD + 导出 |
| **模型** | `/model [alias] [thinking level]` | 切换模型 + thinking，`_apply_thinking_inplace` 优先（零成本），失败回退 rebuild |
| **MCP** | `/mcp [list/tools/test/reload/enable/disable/toggle/config]` | MCP 管理 |
| **上下文** | `/compact`, `/context` | 手动压缩 + 上下文状态 |
| **安全** | `/safety [profile]`, `/approve`, `/reject [reason]` | 安全 profile + HITL 审批 |
| **主题** | `/theme [name]` | 主题切换 |
| **其他** | `/skills`, `/memory`, `/subagents` | 技能/记忆/子代理状态 |

**SlashResult 三种逻辑状态**: `handled`（已处理，显示 lines）→ `passthru`（非斜杠命令，传递给 LLM）→ `rebuild`（agent 非 None，模型/MCP/安全变更需重建图）。

### Input History 多编码兼容

**编码探测序列**: BOM 检测（UTF-8/UTF-16 LE/UTF-16 BE）→ UTF-8 → GBK → CP936 → GB18030 → CP1252 → Latin-1 → UTF-8 with `errors="replace"`

**Up/Down 导航状态机**: `_index: int | None` = None 表示实时输入模式。首次 Up 时 `_draft = current` + `_index = len-1`。Down 越过最新条目后恢复 `_draft`。

**容量限制**: 最多 1000 条，连续重复去重，截头保留最近条目。**读取容忍多编码，写入始终 UTF-8**（渐进式迁移）。

### Welcome 视图 Braille 动画

**实现原理**: 7x5 像素位图 → Unicode Braille 字符（每个 Braille 格编码 4x2 点阵）。`_breathing_intensity()` 计算每个字符亮度：呼吸分量 `sin(2π*t/5.0)` + 涟漪分量（基于距离的正弦波）+ 渐入分量（从中心扩散，0.5s 完全显现）。定时器 12 FPS（~83ms），CSS class `#main.welcome` 控制显示/隐藏。

### Subagent Monitor 状态机

```
SubagentMonitor (线程安全，RLock 保护)
  ├─ start_task() → status="running" (revision += 1)
  ├─ finish_task(error=False) → status="ok"
  ├─ finish_task(error=True) → status="error"
  └─ reset() → 清空所有 runs

SubagentMonitorDialog (0.35s 轮询 snapshot，revision 去重)
  ├─ 左侧列表: task_id / type / wave / depends_on / status
  └─ 右侧详情: description + event 层级树
```

`SubagentMonitorCallback(BaseCallbackHandler)` 注入到子 Agent 的 callbacks 中：`on_llm_end` 捕获 tool_calls → `on_tool_start/end/error` 实时产生 events。通过全局 `_REGISTRY` + `monitor_from_config(config)` 查找 Monitor 实例。
