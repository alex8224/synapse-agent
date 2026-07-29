# Synapse 架构流程文档

> 基于 `src/synapse/**/*.py` 全部源文件分析，版本 v0.1.5
> 侧重**运行时流程与组件交互**，结构总览参见 `docs/architecture.md`
> 第三轮补充：Agent 装配深层 / 中间件算法 / LLM 传输 / 子系统细节（2025-01）

---

## 一、启动流程：从命令行到 TUI 就绪

### 1.1 入口链

```
python -m synapse
  → __main__.py → from synapse.cli import main
  → __init__.py → def main() → cli_main()
  → cli.py → main() 设置 PYTHONUTF8=1 → app()
```

### 1.2 CLI 命令路由

Typer 应用树：

```
app = typer.Typer(name="synapse")
├── [默认回调] @app.callback(invoke_without_command=True)
│   └── _default_tui() → _launch_tui()       # 无子命令时启动 TUI
├── tui     → _launch_tui()                   # 显式 tui 命令
├── version → 打印版本号
├── sessions (子命令组)
│   ├── list / codex-list / codex-inspect / codex-preview / codex-import
│   ├── prune / delete / rename / export / search
├── models  (子命令组)
│   └── list
└── mcp     (子命令组)
    ├── list / test
```

### 1.3 `_launch_tui()` 完整流程

```
_launch_tui()
  ├── _bootstrap_env()
  │   ├── ensure_user_system_prompt()          # 确保 ~/.synapse/system_prompt.md 存在
  │   └── bootstrap_project_env()              # 加载 .env (override=True → project .env 优先)
  │
  ├── _resolve_settings() → load_settings()
  │   ├── bootstrap_project_env() 再次确保
  │   ├── Settings(_env_file=...)  pydantic-settings 初始化
  │   │   └── settings_customise_sources 调整优先级: init → dotenv → env → secrets
  │   ├── load_layered_settings_file()          # 3层配置合并
  │   │   ~/.synapse/settings.json → <exe>/.synapse/settings.json → <project>/.synapse/settings.json
  │   ├── CLI overrides model_copy()
  │   ├── 默认路径决议 (checkpoint/sessions 落到 <project>/.synapse/)
  │   ├── apply_models_config_to_settings()     # models.json 多层合并 → api_key/base_url/thinking
  │   └── bootstrap_theme()                     # 激活 UI 主题
  │
  └── run_tui(settings, thread_id, env_path, project_root)
```

### 1.4 Settings 三层覆盖模型

```
Layer 1:  ~/.synapse/          (user_config_dir)      用户全局
Layer 2:  <exe_dir>/.synapse/  (executable_config_dir) 便携包（可选）
Layer 3:  <workspace>/.synapse/ (project_config_dir)   项目本地
```

后层覆盖前层。配置文件：
- `models.json` — 模型 profiles + api_key
- `mcp.json` — MCP 服务器定义
- `settings.json` — 非敏感设置覆盖（含 theme）
- `themes.json` — 自定义 UI 主题
- `system_prompt.md` — 编码 agent 系统提示

### 1.5 `build_coding_agent()` 装配过程

```
build_coding_agent()                          # agent.py:129
  ├── _apply_observability()                  # LangSmith tracing
  ├── apply_safety_to_settings()              # 安全策略 (dev-autopass / dev-approve / readonly)
  ├── build_backend()                         # CodingLocalShellBackend (UTF-8 safe, pwsh)
  ├── build_model_from_settings()             # ModelRegistry → build_chat_model()
  │   └── 分支: OpenAI (HTTP/WebSocket) / Anthropic / 其他 provider
  ├── apply_harness_exclusions()              # 注册 HarnessProfile.excluded_tools
  ├── build_interrupt_on()                    # require_approval=False → None
  ├── _build_checkpointer()                   # AsyncSqliteSaver (WAL) / MemorySaver
  ├── build_default_subagents()               # 3 种子 Agent: [tester, reviewer, researcher]
  ├── build_filesystem_permissions()          # 只读/拒绝路径
  ├── build_session_tools()                   # list_sessions / read_session
  ├── MCP tools (eager / deferred)            # McpSessionPool → StructuredTool
  ├── middleware 装配 (9 个中间件，按顺序)
  │   └── [DescribeImage, ModelRetry, ToolErrorRecovery, TaskNamespace,
  │        PathNormalize, IntentSchema×2, Steer, Compact, DAG(可选)]
  └── create_deep_agent(model, system_prompt, backend, tools, middleware, ...)
```

### 1.6 TUI 启动：`CodingAgentApp` 创建与初始渲染

```
run_tui()
  ├── SessionStore → pick_startup_thread_id()         # 自动恢复上次会话
  ├── resolve_startup_binding()                       # 恢复会话级模型绑定
  │
  ├── [决策] tui_defer_agent=True（默认）:
  │   agent = None    # 延迟到后台线程构建
  │
  └── CodingAgentApp(agent, settings, thread_id, ...)
      ├── __init__(): 状态、历史、Git、TopBar/BottomBar 注册
      ├── compose(): 三层布局
      │   ├── #topbar       (TopBar, height:1)
      │   ├── #main         (WelcomeView + VerticalScroll#log + TurnRail#turn-rail)
      │   └── #bottom-chrome (SteerQueueWidget + #status + #complete-hint + Input + BottomBar)
      ├── on_mount():
      │   ├── apply_theme()
      │   ├── _refresh_topbar() / _refresh_bottombar()
      │   ├── set_interval(0.1, _tick_status)    # 100ms 状态刷新
      │   └── _bg_build_agent() [@work(thread=True)]  # 后台构建 agent
      │       ├── Phase 1: build_coding_agent(load_mcp=False)
      │       └── Phase 2: attach_mcp_to_agent() (若启用 MCP)
      └── app.run()  # Textual 事件循环启动
```

---

## 二、Agent 运行时循环：一次用户对话的完整数据流

### 2.1 入口：从用户输入到 agent.astream

```
用户输入 (TUI Input.on_submit)
  → run_turn() [@worker 线程]
    ├── compose_user_content()            # 多模态粘合 (ImageBank)
    ├── append_user()                     # UserTurnBlock → _refresh_turn_rail()
    └── stream_agent(agent, payload, config, sink=TextualStreamSink)
        └── _iter_stream_events()
            → agent.astream(payload, config,
                 stream_mode=["messages", "updates"],
                 subgraphs=True, version="v2")

AsyncSqliteSaver 路径:
  → async_runtime.py 进程级守护事件循环
  → asyncio.run_coroutine_threadsafe(_main(), bound_loop)
MemorySaver 路径:
  → threading.Thread + asyncio.run(_main())
```

### 2.2 中间件链执行顺序

中间件在 `build_coding_agent()` 中按顺序注入，LangGraph 执行时依序调用：

| 序号 | 中间件 | 类型 | 作用 |
|------|--------|------|------|
| 1 | **DescribeImageMiddleware** | model-call | 图片→文字（非视觉模型时调用 VisionModelClient） |
| 2 | **NotifyingModelRetryMiddleware** | model-call | 429/5xx 重试，指数退避 (1s→8s max)，通知 UI |
| 3 | **ToolErrorRecovery** | tool-call | 工具异常→ToolMessage(content="Error: ...")，不终止图 |
| 4 | **TaskNamespace** | tool-call | task() 调用注入子图隔离 checkpoint_ns |
| 5 | **PathNormalize** | tool-call | 主机绝对路径→/虚拟路径映射 |
| 6 | **IntentSchema×2** | model+tool-call | 工具注入必填 intent 字段，执行时剥离 |
| 7 | **SteerMiddleware** | model-call | 排空 SteerQueue 队列，作为 [Mid-run user guidance] 注入 |
| 8 | **CompactTool** | model-call | compact_conversation 工具 + 自动压缩 (85% token 阈值) |
| 9 | **DAGSubAgentMiddleware** | model+tool-call | task() DAG 拓扑排序→波次并行子 Agent（可选） |

### 2.3 LLM 推理分支

```
models_registry.py → build_chat_model()

├─ model_name.startswith("openai:")
│   ├─ enable_openai_compat_reasoning_patch()   # DeepSeek reasoning_content monkey-patch
│   ├─ build_openai_async_http_client()          # 每模型独立 httpx.AsyncClient (300s keep-alive)
│   ├─ if websocket == True:
│   │     → ResponsesWebSocketChatOpenAI          # 持久 WS 连接，复用消息/块转换层
│   └─ else:
│         → init_chat_model("openai:xxx")         # ChatOpenAI (HTTP SSE 流)
│
├─ model_name.startswith("anthropic:"):
│   └─ init_chat_model("anthropic:xxx")          # ChatAnthropic
│
└─ 其他 provider:
    └─ init_chat_model(model_name)                # 任意 LangChain 支持的 provider
```

### 2.4 工具执行流程

```
AIMessage(tool_calls=[...])
  → LangGraph ToolNode 路由
    ├─ read_file / write_file / edit_file / glob / grep
    │   → FilesystemMiddleware → CodingLocalShellBackend
    │   → grep 使用 ripgrep (UTF-8 安全包装)
    │   → glob/read 受 ToolIgnoreMatcher (.gitignore 规则) 过滤
    │
    ├─ execute → CodingLocalShellBackend._execute()
    │   → subprocess.Popen (pwsh: -NoProfile -NonInteractive)
    │   → communicate(timeout) → kill_process_tree (Windows: taskkill /T)
    │
    ├─ task → DAGSubAgentMiddleware / SubAgentMiddleware
    │   → 编译的子 Agent 子图独立执行
    │
    ├─ MCP tools → McpSessionPool (stdio/SSE/HTTP)
    │
    ├─ list_sessions / read_session → SessionStore (SQLite)
    │
    └─ compact_conversation → SummarizationMiddleware
```

### 2.5 流式事件处理

```
_iter_stream_events 返回 (mode, chunk, namespace) 元组流

mode == "messages":          # 实时 token 流
  ├─ reasoning_delta → sink.write_reasoning(text)
  ├─ content_text    → sink.write_answer_token(text)
  └─ tool_call_chunks → sink.activity_update("tool", ...)

mode == "updates":           # 完整消息
  ├─ ToolMessage → sink.tool_item_finished() + sink.tool_group_closed()
  ├─ AIMessage   → 提取 reasoning / text / tool_calls
  │   ├─ tool_calls → sink.tool_calls_started() / sink.tool_item_started()
  │   └─ text       → sink.write_answer_complete()
  └─ 用量累积 (dedupe by msg_id)

mode == "__heartbeat__":
  └─ sink.activity_update(phase, detail)        # 保持 "running" 状态

mode == "__cancelled__":
  └─ cancel_repair.repair_thread_after_cancel()  # 密封 open tool_calls + 终止标记
```

### 2.6 结果持久化

```
agent.astream 完成后:
  ├── LangGraph AsyncSqliteSaver 自动保存 (每个 super-step)
  │   └── DeltaChannel 增量存储 (O(N) 而非 O(N²))
  │
  ├── SessionStore.touch():
  │   → ensure(): 新会话 INSERT
  │   → UPDATE: updated_at / title (首条用户消息) / model / thinking
  │
  ├── AutoRecorder.record_if_valuable():
  │   ├── 阶段1: _is_trivial() 快速拒绝 (问候/超短)
  │   ├── 阶段2: _has_knowledge_signals() (bug/fix/config/def/class)
  │   ├── 阶段3: _extract_lessons() (LLM提取 或 关键词启发式，最多3条)
  │   └── 阶段4: ltm.remember() → LongTermMemory (SQLite + 向量 BLOB)
  │
  └── SummarizationMiddleware (token ~85% 阈值触发):
      → 裁剪最早消息 → 模型生成摘要
      → 写入 conversation_history/{thread_id}.md
```

### 2.7 取消修复机制

```
用户按 ESC → cancel_event.set()
  → _iter_stream_events 检测 → mode == "__cancelled__"
  → repair_thread_after_cancel(agent, config):
      1. agent.get_state(config) → 获取当前快照
      2. _pending_tool_seals(): 为未完成的 tool_calls 生成 ToolMessage("[cancelled by user]")
      3. agent.update_state(config, {"messages": seals}, as_node=tools_node)
      4. _needs_cancel_note(): 若轮次未闭合 → 插入 AIMessage("[本轮已由用户终止]")
      5. 验证 state 完整性
```

---

## 三、TUI 层架构

### 3.1 Screen 布局

```
CodingAgentApp(App)
├── #topbar (height:1, 固定)
│   └── TopBar: 左(工作区) 中(标题/分支) 右(用量/上下文%)
│       └── GitChangesPopover (hover 时浮现脏文件列表)
│
├── #main (height:1fr, 弹性)
│   ├── WelcomeView           (无历史时: Braille "SYNAPSE" 动画)
│   ├── VerticalScroll#log    (主时间线: UserTurnBlock / ThoughtBlock / ToolGroupBlock / AnswerBlock)
│   ├── TurnRail#turn-rail    (dock:right, overlay, width:34: 轮次导航刻度)
│   └── Static#stream         (display:none, 历史遗留)
│
└── #bottom-chrome (dock:bottom, auto)
    ├── SteerQueueWidget      (中程引导队列, 默认隐藏)
    ├── Static#status         (活动指示器)
    ├── Static#complete-hint  (Tab 补全下拉, 最多6行)
    ├── Input#prompt          (主输入框, height:3)
    └── Static#bottombar      (模型名 / MCP状态 / 快捷键 / 模式)
```

### 3.2 流式桥梁 TextualStreamSink

**核心机制**：Agent 工作线程通过 `call_from_thread()` 安全推送到 Textual 主线程。

**自适应节流**：
- 输出 < 3K 字符：0.12s 间隔
- 输出 3K~12K 字符：0.25s 间隔
- 输出 >= 12K 字符：0.40s 间隔

**尾部预览优化**：流式推送只传最后 28 行/3500 字符的纯文本预览，完整 Markdown 渲染仅在 `commit_answer`/`commit_thought` 时执行一次。

**子代理活动去重合并**：0.25s 最小间隔 + `_queue_subagent_activity()` 去重，避免高频嵌套事件冻结 UI。

| Sink 方法 | TUI 方法 | 效果 |
|-----------|----------|------|
| `write_reasoning(text)` | `set_stream("reasoning", ...)` | ThoughtBlock (live) |
| `close_reasoning()` | `commit_thought(body)` | ThoughtBlock 封存 (可折叠) |
| `write_answer_token(text)` | `set_stream("answer", ...)` | AnswerBlock (live) |
| `write_answer_complete(text)` | `commit_answer(body)` | Markdown 渲染 + 封存 |
| `tool_item_started(item)` | `write_tool_group_header` + `write_tool_item` | ToolGroupBlock |
| `tool_item_finished(id)` | `update_tool_item` + `update_tool_group_header` | 刷新图标/状态 |
| `tool_group_closed(gid)` | `close_tool_group` | 冻结 tool block |
| `note_usage(...)` | `apply_turn_usage(...)` | TopBar 用量更新 |
| `activity_start/update/stop` | `set_activity(...)` | #status + subtitle |

### 3.3 Dialog 系统

所有对话框继承 `DialogBase(ModalScreen)`，统一模板：dialog-window (宽66, 高28, border round) → DialogBody (VerticalScroll)。

| 对话框 | 快捷键 | 用途 |
|--------|--------|------|
| ModelPickerDialog | F2 | 切换模型/thinking 级别 |
| ThemePickerDialog | F3 | 切换 UI 主题 |
| SessionListDialog | F4 | 会话列表/切换/删除/搜索 |
| McpPanelDialog | F5 | MCP 服务器状态/启用/禁用 |
| SafetyPanelDialog | F6 | 安全设置 (allow/ask/deny) |
| CodexSessionListDialog | F7 | 从 Codex CLI 导入会话 |
| ThemeDesignerDialog | F8 | HSL 调色板主题编辑器 |
| SubagentMonitorDialog | F9 | 子代理实时监控 |
| SelectableTextModal | Ctrl+Shift+S | 对话纯文本导出 |
| GitExploreScreen | 分支点击 | Git 工作区/暂存区 diff 浏览 |

### 3.4 斜杠补全与输入历史

**补全系统** (`slash_complete.py`)：
- `/` 开头 → 46 个 ROOT_COMMANDS + 子命令 + 动态补全
- `/model <prefix>` → 模型注册表别名
- `/session <sub>` → 会话列表动态候选
- `@` 在输入中 → 文件系统路径补全
- Tab 循环 + ghost suggestion 优先接受

**输入历史** (`input_history.py`)：
- 最多 1000 条，多编码兼容 (UTF-8/GBK/CP936/GB18030)
- Up/Down 导航，补全菜单活跃时被重定向为补全导航

**SteerQueue 中程引导**：
- Agent 运行中在 Input 中键入文本 → SteerQueue.push(text)
- 中间件每次 LLM 调用前排空队列 → `[Mid-run user guidance]` HumanMessage
- UI: SteerQueueWidget 显示队列项，可单独/批量删除
- turn 完成时若有未消费引导 → 自动启动 follow-up steer turn

---

## 四、子 Agent DAG 调度

### 4.1 调度流程

```
DAGSubAgentMiddleware.awrap_model_call()
  → 提取 task tool_calls
  → _capture_parent_run_config()              # 捕获父图跟踪上下文
  → _execute_dag()
    → 解析 tasks: [{subagent_type, description, task_id, depends_on}]
    → while remaining:
        ├── _topological_waves()              # 筛选依赖已满足的任务
        │   → ready = [t for t in pending if depends_on ⊆ completed]
        ├── 死锁检测: ready==[] → ValueError
        ├── batch = ready[:max_parallel]      # 限流分批
        ├── asyncio.gather(*wave_coros)       # 波次内并行
        └── 收集 results[task_id] = output_text
    → 结果缓存到 _dag_cache[tool_call_id]
  → awrap_tool_call 拦截 task → 返回缓存结果 ToolMessage
```

### 4.2 单任务执行

```
_run_one_subagent(type, description, deps_completed)
  → _subagent_runnables[type] 取预编译子图
  → _enrich_description(): 注入上游依赖输出到 description
  → _build_subagent_run_config(): checkpoint_ns 隔离 + LangSmith 追踪
  → runnable.ainvoke({"messages": [HumanMessage(description)]})
  → _extract_final_response(): 最后一条无 tool_calls 的 AIMessage 文本
```

### 4.3 三种子 Agent 差异

| 维度 | researcher | tester | reviewer |
|------|-----------|--------|----------|
| write_file/edit_file | 禁用 | 启用 | 禁用 |
| execute 命令 | 禁用 | 启用 | 启用 |
| 独立模型 | `researcher_model` | `tester_model` | `reviewer_model` |
| 角色定位 | 只读代码分析 | 测试执行与诊断 | 代码审查 |

---

## 五、MCP 集成

### 5.1 架构

```
McpSessionPool (进程级单例 _ACTIVE_POOL)
  └── _LoopThread (守护线程, 独立 asyncio 事件循环)
      └── _loop.run(coro)  跨线程同步等待

load_mcp_tools(servers)
  → McpSessionPool.load(servers)
    → _discover(): 并行连接所有 server
      └── _open_one(server):
          ├─ transport="stdio" → stdio_client(StdioServerParameters)
          ├─ transport="sse"   → sse_client(url, headers)
          └─ transport="streamable_http"/"http" → streamablehttp_client(url, headers)
    → session.list_tools() → [MCP Tool]
    → _make_tool(): JSON Schema → Pydantic BaseModel → StructuredTool
```

### 5.2 配置加载

```
load_mcp_server_configs(path=..., workspace=...)
  → 层级覆盖: ~/.coding-agent/mcp.json → <workspace>/.coding-agent/mcp.json
  → 按 name 去重（后者覆盖前者）
  → 返回 [McpServerConfig]
```

---

## 六、记忆系统

### 6.1 AutoRecorder 三阶段过滤

```
record_if_valuable(ltm, task, answer, thread_id)
  ├── 阶段1: _is_trivial() — 问候/超短/单命令 → 拒绝
  ├── 阶段2: _has_knowledge_signals() — 正则匹配 fix/bug/config/def/class
  │   └── 无信号但 answer >= 400 字符 → 放行
  ├── 阶段3: _extract_lessons()
  │   ├── 优先 LLM 提取 (调用 cheap model, 最多3条, 每条≤60汉字)
  │   └── 回退关键词启发式 (IMPORTANCE_MARKERS 打分, 取前3条)
  └── 阶段4: ltm.remember() → LongTermMemory
```

### 6.2 LongTermMemory 存储与检索

```
存储:
  remember(text, metadata) → embedder.embed([text])
  → struct.pack('<Nf', ...) → BLOB
  → INSERT INTO memories (id, text, metadata, embedding, created_at)

检索:
  recall(query, top_k=5, min_similarity=0.0)
  → embedder.embed([query])
  → SELECT * → 逐行 _unpack_floats()
  → _cosine_similarity(query_vec, stored_vec) 排序
```

### 6.3 Embedder

| 实现 | 模型 | 维度 | 依赖 |
|------|------|------|------|
| `LocalEmbedder` | `all-MiniLM-L6-v2` | 384d | `sentence-transformers` |
| `SimpleEmbedder` | TF-IDF 词袋 | 256d | 零依赖 |

工厂函数优先 `LocalEmbedder`，ImportError 时回退 `SimpleEmbedder`。

---

## 七、技能系统

### 7.1 发现流程

```
discover_skills(skills_paths) → [SkillInfo]
  → for root in skills_paths:
      rglob("SKILL.md")
  → _parse_frontmatter(text):
      └─ 正则提取 ---...--- YAML 块 → name / description / license / allowed-tools
```

### 7.2 注入路径

```
create_deep_agent(skills=skills_paths)
  → deepagents 内部将 SKILL.md 内容注入 Agent system prompt
  → /skills 斜杠命令可查看已发现的技能列表
```

---

## 八、安全模型（三层边界）

### 第一层：SafetyProfile (`safety.py`)

```
dev-autopass: require_approval=False, readonly=False, auto_approve=True
dev-approve:  require_approval=True,  readonly=False, auto_approve=False
readonly:     require_approval=False, readonly=True,  auto_approve=True
```

命令黑名单 (`DANGEROUS_PATTERNS`): 14 个正则，覆盖 `rm -rf /`、`git push --force`、`shutdown`、fork 炸弹等。

### 第二层：HITL 审批 (`hitl.py`)

```python
extract_pending_interrupt(agent, config) → PendingInterrupt
# TUI/CLI 通过 /approve 或 /reject 斜杠命令响应
# → Command(resume=...) 或 agent.ainvoke(None, config, interrupt_before=...)
```

### 第三层：FilesystemPermission (`fs_permissions.py`)

- `LocalShellBackend` 下默认返回 `None`（避免冲突）
- 只读通过 harness 工具排除实现
- 可配置 `deny_paths` 保护敏感文件

---

## 九、RAG 知识库

```
ProjectKnowledgeBase(project_root, embedder, max_files=300)

index(force=False):
  → _discover_docs(): rglob(.md/.py/.rst/.txt/.toml/.yaml/.yml)
  → 排除: .git, .venv, node_modules, __pycache__
  → _chunk(text, max_chars=1500): 按段落边界(\n\n)分割，合并直到max_chars
  → embedder.embed(chunks) → vectors
  → _upsert_chunk(): INSERT OR REPLACE → knowledge.sqlite

search(query, top_k=5, min_similarity=0.0):
  → embedder.embed([query]) → SELECT * → _cosine_similarity → 排序截断
```

---

## 十、Codex 导入（三阶段流水线）

### 阶段 1: Scanner — `CodexSessionScanner`

```
扫描 Codex 的 state_*.sqlite → 匹配 native_id
→ 查找 rollout-*.jsonl[.zst]
→ 解析标题 → 验证 workspace → 返回 CodexSession
```

### 阶段 2: Projector — `CodexHistoryProjector`

```
project_path(rollout_path):
  → 逐行解析 JSONL (支持 zstandard 压缩)
  → 提取 event_msg.user_message / agent_message
  → 忽略: bare response_item、subagent 内部消息
  → 返回 CodexTextSnapshot (messages, importable 标志)
```

### 阶段 3: ImportService — `CodexImportService`

```
import_codex_session(native_id, ...):
  ├── 阶段1: Scanner.inspect()
  ├── 阶段2: Projector.project_path()
  └── 阶段3: ImportService.import_snapshot()
      ├── snapshot_digest(): SHA256 哈希
      ├── ledger.claim() 租约机制:
      │   ├── "new"      → CheckpointSeeder.seed_snapshot() + SessionStore.ensure()
      │   ├── "completed" → 幂等复用
      │   └── "recover"   → 验证 → 补偿清理 → 重新 seed
      └── 异常 → _compensate_new(): 回滚 SessionStore + Checkpoint + Ledger
```

租约机制：`CodexImportLedger` (SQLite, 120s 租约)，防止并发导入，支持崩溃恢复。

---

## 十一、完整交互时序总览

```
用户输入 Submit (idle)
  │
  ├─ [斜杠命令] → handle_slash() → 本地处理 / rebuild agent / 恢复会话
  │
  └─ [普通消息]
      ├─ append_user() → UserTurnBlock → TurnRail 刷新
      └─ run_turn() [@worker]
          ├─ TextualStreamSink(self)  ← 流式桥梁
          └─ stream_agent() [agent 工作线程]
              │
              ├─ [中间件链: awrap_model_call]
              │   DescribeImage → ModelRetry → Steer → DAG → Compact
              │
              ├─ [LLM 推理]
              │   OpenAI (HTTP/WS) / Anthropic
              │
              ├─ [ToolNode: 工具执行]
              │   read/write/edit/glob/grep/execute → CodingLocalShellBackend
              │   task → DAG 拓扑 → 波次并行子 Agent
              │   MCP tools → McpSessionPool
              │
              ├─ [流式推送 → TUI]
              │   reasoning → ThoughtBlock (live/sealed)
              │   tool_calls → ToolGroupBlock (聚合)
              │   answer → AnswerBlock (Markdown 渲染)
              │   usage → TopBar 更新
              │
              └─ [用户取消 ESC]
                  → repair_thread_after_cancel()
                    密封 open tool_calls + 终止标记

  → _turn_done()
      ├─ SessionStore.touch()     # 更新会话元数据
      ├─ AutoRecorder.record()    # 提取长期记忆
      ├─ _refresh_git_chrome()    # 刷新 Git 脏标记
      └─ _maybe_followup_steer()  # 自动消费未处理的 SteerQueue
```

---

## 十二、补充流程细节

### TUI 流式推送优化

**自适应节流**（`TextualStreamSink._stream_interval()`）：

| 缓冲区大小 | 推送间隔 | 适用场景 |
|------------|----------|----------|
| < 3,000 字符 | 0.12s | 短回复 |
| 3,000 ~ 12,000 | 0.25s | 中等回复 |
| >= 12,000 | 0.40s | 长回复 |

**尾部预览优化**：流式推送只传最后 28 行/3500 字符的纯文本预览（`stream_tail_preview()`），完整 Markdown 渲染仅在 `commit_answer`/`commit_thought` 时执行一次。超过 24,000 字符的 Markdown 直接降级为纯文本渲染。

**子代理 Activity 去重合并**：
- 0.25s 最小推送间隔
- 过滤 `ns=...` 原始 namespace 字符串
- 将 `"streaming nested tokens"` 和 `"waiting for model"` 视为噪声，保持上次有效状态
- 若 `detail` 与上次相同 → 丢弃
- `_queue_subagent_activity()` 队列延迟到 0.25s 后才真正推送

**`call_from_thread` 机制**：Agent 工作线程通过 `app.call_from_thread(fn, *args, **kwargs)` 安全推送到 Textual 主线程。若 `RuntimeError`（app 未运行）则直接同步调用作为降级。

### DAG 子 Agent 调度补充

**死锁检测**（`_execute_dag_inner`）：
```
while remaining:
    ready, remaining = _topological_waves(remaining, completed_ids)
    if not ready and remaining:
        raise ValueError("DAG 死锁：以下任务依赖了不存在的或循环引用的 task_id...")
```

**限流**：`batch = ready[:self._max_parallel]`，每波最多并行 `max_parallel` 个（默认 6，通过 `AGENT_MAX_PARALLEL_SUBAGENTS` 配置）。

**结果缓存与消费**：
```
awrap_model_call:
  → 提取 task tool_calls
  → _execute_dag() 预执行所有任务
  → _dag_cache[tool_call_cache_key] = tool_content   # 缓存结果

awrap_tool_call:
  → 拦截 task 工具调用
  → 直接从 _dag_cache.pop(key) 返回预计算好的 ToolMessage  # 避免二次执行
```

**上游依赖注入**：`_enrich_description(task, upstream_results)` 将上游任务输出作为上下文注入到子 Agent 的 description 中，格式为 `## 依赖任务 [dep_id] 的输出\n\n{result}\n\n---\n\n## 你的任务\n\n{base_desc}`。

### 取消修复完整步骤

```
repair_thread_after_cancel(agent, config):
  STEP 1: 状态快照
    snap = agent.get_state(config)
  
  STEP 2: 提取消息
    messages = list(snap.values.get("messages") or [])
  
  STEP 3: 获取图结构
    nodes = agent.nodes.keys()
    next_nodes = tuple(snap.next or ())
  
  STEP 4: 密封未完成工具调用
    seals = _pending_tool_seals(messages)  # 每个无 ToolMessage 的 tool_call → "[cancelled by user]"
    if seals:
      agent.update_state(config, {"messages": seals}, as_node=tools_node)
  
  STEP 5: 终止标记
    if _needs_cancel_note(messages):
      note = AIMessage(content="[本轮已由用户终止，上下文已保留]")
      agent.update_state(config, {"messages": [note]}, as_node=model_node)
  
  STEP 6: 验证
    重新获取 snap 确认 messages 和 next_nodes 完整性
```

**`_needs_cancel_note` 判断逻辑**：
- 最后一条是 AI 消息且无 tool_calls + 内容非空 → 回合已闭合，无需标记
- Human 无回答 / Tool 无跟随 AI / AI 有未完成 tool_calls → 需要终止标记

### Checkpointer 线程模型对比

| 维度 | `AsyncSqliteSaver`（默认） | `MemorySaver` |
|------|-------------------------|-------------|
| 存储 | SQLite 文件（WAL 模式） | 纯内存 dict |
| 跨 turn 持久 | 是 | 否 |
| 事件循环 | 必须在创建它的同一 loop 上操作 | 无约束 |
| AsyncRuntime 依赖 | 强依赖（所有操作通过 `runtime.submit()` 调度） | 无需 |
| `get_tuple()` 行为 | `run_coroutine_threadsafe` 代理到 runtime loop | 直接同步调用 |
| 配置键 | `checkpoint_backend="sqlite"` | `checkpoint_backend="memory"` |

**AsyncSqliteSaver 数据流**：
```
主线程（TUI/CLI）
  → runtime.run(saver.open())  →  AsyncRuntime 守护线程
  → runtime.run(agent.astream()) →  同一 loop 执行
  → runtime.run(saver.get_tuple()) → 同一 loop 读取 checkpoint
```

**MemorySaver 数据流**：
```
主线程（任意线程）
  → MemorySaver()  同步创建
  → agent.astream()  直接在调用线程的事件循环中执行
```

### AutoRecorder 集成状态

`AutoRecorder` 目前**尚未实际集成到 Agent 运行时中**（`agent.py` 无引用）。设计意图是在每轮对话后通过 `record_if_valuable()` 三阶段过滤提取教训并存入 `LongTermMemory`，但此功能仍处于待接入状态。

**三阶段过滤逻辑**（已实现但未激活）：
- Stage 1: `_is_trivial()` — 问候/超短/斜杠命令 → 拒绝
- Stage 2: `_has_knowledge_signals()` — 正则匹配 fix/bug/error/solution/config 等关键词；无信号但 answer >= 400 字符 → 放行
- Stage 3: `_extract_lessons()` — LLM 提取（最多 3 条，每条 ≤60 汉字）或关键词启发式回退

### CodingLocalShellBackend 的 Windows 兼容设计

`CodingLocalShellBackend` 覆写 `execute()` 的主要原因：上游 `LocalShellBackend` 使用 `text=True` 不指定 `encoding`，在中文 Windows 上常以 GBK 解码 UTF-8 工具输出而崩溃。

**三层防御**：
1. 所有 subprocess 调用显式传递 `encoding="utf-8"` + `errors="replace"`
2. 子进程环境预设 `PYTHONUTF8=1` + `PYTHONIOENCODING=utf-8`
3. ripgrep 搜索覆写使用相同编码策略，JSON 解析失败/UnicodeDecodeError 时降级到 Python fallback

**进程树 Kill**（`_kill_process_tree`）：Windows 上 `Popen.kill()` 只终止直接进程，子进程可能持有管道句柄导致 `communicate()` 永久挂起。解决方案：`taskkill /T /F /PID {pid}` 全树终止 + `proc.communicate(timeout=3)` 排空管道。

---

## 十三、第二轮补充：Slash 补全 / Git / Transcript / MCP 生命周期 / Vision / WS 传输

### Slash 补全引擎状态机

**`complete_slash()` 决策流程**:
```
输入 value (当前输入文本), ctx: SlashCompleteContext
  ├─ 不以 "/" 开头 → 返回 []
  ├─ 无空格 → filter 26 个 ROOT_COMMANDS by prefix
  ├─ /session: SESSION_SUBCOMMANDS → 动态会话列表 → EXPORT_FORMATS
  ├─ /switch/: 所有会话 (id优先于title匹配)
  ├─ /model/: 模型别名 + thinking levels（支持简写 /model alias high）
  ├─ /mcp/: MCP_SUBCOMMANDS (list/tools/test/reload/enable/disable/toggle/config)
  ├─ /theme/: 动态加载 theme_names
  └─ /safety/: [dev-autopass, dev-approve, readonly, hitl, auto, ro]
```

**动态补全数据源**: `SlashCompleteContext` 从 `SessionStore`、`registry_from_settings`、`load_mcp_server_configs` 三个数据源填充，所有异常静默处理。

**Ghost 优先 vs Tab 循环**: `SlashSuggester.get_suggestion()` 返回第一个候选（ghost text）。Tab → 接受 ghost；再次 Tab → `cycle_completion` 循环到下一候选。Shift+Tab 回退。Ctrl+Space 弹出候选列表。

**`@` 文件路径补全**: BFS 递归搜索，跳过 19 个 `_SKIP_DIRS`（.git/node_modules/__pycache__ 等），最大扫描 2000 条，目录优先+字母序。支持 directory mode（`@src/`）和 file/partial mode（`@src/main`）。

### Git 集成的异步探测与 diff 降级链

**`probe_git_branch_chrome()` 四步探测流水线**:
```
Step 1: git rev-parse --abbrev-ref HEAD          → 分支名 (HEAD=detached→None)
Step 2: git status --porcelain                    → dirty 标记
Step 3: git rev-list --left-right --count ...     → ahead/behind
Step 4: git diff HEAD --shortstat                 → files/inserted/deleted
```
每步独立容错，失败不影响其他步骤。全部命令 `timeout=0.8s` 硬时限防 UI 卡顿，编码 `errors="replace"`。

**`probe_git_changed_files()` 三数据源组合**:
```
Source A: git status --porcelain=v1   → 路径 + XY 状态码 (含 ?? 未跟踪)
Source B: git diff --numstat          → 未暂存区 +A -D 行数
Source C: git diff --cached --numstat → 暂存区 +A -D 行数
```
合并 A+B+C，上限 40 个文件。`_status_letter()` 将 XY 双字母压缩为单字母（优先 worktree 侧）。

**GitChangesPopover 悬停状态机**:
```
_branch_hover + _popover_hover 双布尔状态:
  MouseMove on branch → show_popover()，cancel hide timer
  MouseMove off branch → schedule_hide(120ms)
  _hide_popover(): 仅当两者都为 False 才移除
_popover_seq 递增去重，_purge_screen_popovers() 防 DuplicateIds
```

**Diff 渲染两级降级**:
```
make_diff_view() → textual_diff_view (左右双面板, split=True)
  ├─ 不可用 (binary/error/库缺失) → fallback_renderable()
  │   └─ render_unified_diff() → Python difflib.unified_diff + Rich 着色
  └─ 容量保护: 512KB / 8000 行截断，二进制用 NUL 字节检测
```

### Transcript 三级回退加载

**`load_thread_messages()` 回退链**:
```
Level 1: agent.get_state(config) → state.values["messages"]
Level 2: checkpointer.get_tuple(config) → checkpoint.channel_values["messages"]
         ├─ 空则沿 parent_config 链回溯（最多 50 层）
         └─ 设计意图：SummarizationMiddleware 清空当前 checkpoint 后回退到压缩前的父 checkpoint
Level 3: SqliteSaver + parse_conversation_history_md(
           conversation_history/{thread_id}.md)  # deepagents 压缩导出
```

**`parse_conversation_history_md()` 解析规则**: 正则 `<message type="(human|ai|tool)">(.*?)</message>` 切分消息，AI 消息内部解析 `` 标签。兼容 deepagents SummarizationToolMiddleware 的导出格式。

**`fold_messages_for_ui()` 三层过滤**:
1. `is_lc_summarization_message()` — 检查 `additional_kwargs["lc_source"] == "summarization"`
2. `is_context_compact_text()` — 匹配 SESSION INTENT + SUMMARY 等压缩头部
3. `_NON_TEXT_BLOCK_TYPES` — 黑名单 tool_use/tool_call/tool_result/reasoning/thinking/image 等 block 类型

### MCP 连接生命周期与错误恢复

**连接建立流程**:
```
load_mcp_tools(servers)
  → McpSessionPool.load(servers)
    → _discover(): 并行 _open_one() 所有 server
      → transport 路由: stdio (subprocess) / sse / streamable_http
      → session.initialize()
      → session.list_tools() → _make_tool() × N → StructuredTool
```

**错误恢复链**:
```
_call() 异常 → 立即 _servers.pop(name) 移除死连接 → 返回错误消息
close(): 先置空 tools/tool_names → 逐个 close session → close transport → stop loop
```

**原子替换策略**: `load_mcp_tools()` 在 `_POOL_LOCK` 下将 `_ACTIVE_POOL` 原子替换。旧池在锁外关闭（避免死锁）。加载异常时 `pool.close()` 并返回空结果 + warning。

**`_LoopThread` 超时保护**: `run(coro, timeout=120)` → `Future.result(timeout)` → 超时时 `Future.cancel()` + 线程死亡检测（`is_alive()` False 则抛 RuntimeError）。

### Vision 管线三种场景完整转换链

**场景 1 — 原生视觉模型（GPT-4o）**: 图片 → ImageBank → `compose_user_content()` → `image_url` blocks → DescribeImageMiddleware 透传 → LLM 收到多模态 content

**场景 2 — 纯文本模型（DeepSeek V3）**: 图片 → ImageBank → compose → DescribeImageMiddleware 拦截 → VisionModelClient.describe_data_url() → `[image]\n...描述...\n[/image]` → LLM 收到纯文本

**场景 3 — 无 Vision Model**: client=None → `[image unavailable: automatic description failed]` → LLM 收到占位文本

**Provider 自适应**: `_image_block()` 根据 provider 生成不同格式：
- OpenAI: `{"type":"image_url","image_url":{"url":"data:image/png;base64,..."}}`
- Anthropic: `{"type":"image","source":{"type":"base64","media_type":"image/png","data":"..."}}`
- Google: 同 OpenAI 格式

**`[image]...[/image]` 包装格式设计意图**: 结构化标记让 LLM 区分"用户说的"和"系统插入的图片描述"，在复杂多轮对话中防止语义混淆。

### WebSocket vs HTTP SSE 传输差异

| 维度 | HTTP SSE | WebSocket |
|------|----------|-----------|
| 连接模式 | 每次请求新建 HTTP 连接 | 持久化 WebSocket 连接 |
| 延迟 | TCP+TLS ~50-100ms | 无握手 <1ms |
| 适用场景 | 低频请求 | 高频连续请求（TUI 交互） |
| 协议格式 | SSE (text/event-stream) | "response.create" 事件 (JSON) |
| 串行化 | 天然支持 | 需 `_responses_ws_lock` 串行化 |
| 重连策略 | 无状态 | 异常时 `_reset_responses_websocket()` 自动重建 |

**`_astream_responses()` 核心引擎**:
```
请求构建 → prepare_responses_websocket_event() 格式转换 → WS 发送
  → 事件接收循环 (带 stream_chunk_timeout 超时)
  → _convert_responses_chunk_to_generation_chunk() 事件→Chunk (状态桥接)
  → yield ChatGenerationChunk
  → 终端事件 (response.completed/failed/incomplete) 跳出
```

**错误处理**: `event_type=="error"` → RuntimeError；`BaseException`（取消/协议错误/断开）→ `_reset_responses_websocket()` 重建连接 → 向上重新抛出。

### TaskPlanner 架构（待集成）

`TaskPlanner.plan()` 快速启发式：`len(task.split()) < 6` → 直接单步返回（零 LLM 调用）。复杂任务 → `_llm_decompose()` → `_parse_steps()` 正则提取 JSON 数组。`min_steps_for_plan=3`（少于 3 步视为不复杂），`max_plan_steps=8`（截断上限）。目前此功能**已实现但未集成到主 Agent 循环**（仅 `agent._coding_planner_model` 存储引用）。

---

## 十四、第三轮补充：Agent 装配深层 / 中间件算法 / LLM 传输 / 子系统细节

### Model 缓存 LRU 淘汰机制

`build_coding_agent()` 中 `model_cache` 上限为 **8 个 model 实例**（agent.py 行 190-198）：

```python
if len(model_cache) >= 8:
    evicted = model_cache.pop(next(iter(model_cache)))  # dict 插入序首个（最老）
    close_model_async_http_client(evicted)               # 关闭旧 model 的 AsyncClient
model_cache[cache_key] = model
```

`cache_key` = `model_cache_key(settings, model_name)`，按 `(active_model, enable_thinking, reasoning_effort)` 三元组区分。缓存命中时跳过 model 构建和 `enable_openai_compat_reasoning_patch()`，只重新获取 `ModelRegistry`（因 MCP/theme 变化可能影响 registry）。

### `create_deep_agent` 内部中间件堆栈完整顺序

`create_deep_agent` 在用户 middleware 前后分别插入内置堆栈（由 deepagents SDK 内部控制）：

```
[SDK Base Stack — 最先执行，最外层]
  1. TodoListMiddleware              # 管理 todo 列表
  2. SkillsMiddleware                # 条件：skills 非 None
  3. FilesystemMiddleware            # 文件系统工具 + permissions
  4. SubAgentMiddleware              # 条件：有同步 subagents（声明式/预编译）
  5. SummarizationMiddleware         # 自动摘要
  6. PatchToolCallsMiddleware        # tool call 补丁
  7. AsyncSubAgentMiddleware         # 条件：有异步 subagents

[Synapse User Stack — 中间层]
  8. DescribeImageMiddleware         # 图片→文字
  9. NotifyingModelRetryMiddleware   # 重试 + 指数退避
 10. ToolErrorRecoveryMiddleware     # 工具异常→ToolMessage
 11. TaskNamespaceMiddleware         # 子图 checkpoint_ns 隔离
 12. PathNormalizeMiddleware         # 主机路径→虚拟路径
 13. IntentSchemaMiddleware ×2       # intent 注入 + 剥离
 14. SteerMiddleware                 # 中程引导注入
 15. CompactToolMiddleware           # compact_conversation 工具（条件）
 16. DAGSubAgentMiddleware           # DAG 并行调度（条件）

[SDK Tail Stack — 最后执行，最内层]
 17. HarnessProfile.extra_middleware # 条件：有排除工具
 18. _ToolExclusionMiddleware        # 从 ModelRequest.tools 过滤
 19. AnthropicPromptCachingMiddleware# 无条件（非 Anthropic 模型 no-op）
 20. BedrockPromptCachingMiddleware  # 条件：langchain-aws 可用
 21. MemoryMiddleware                # 条件：memory 非 None
 22. HumanInTheLoopMiddleware        # 条件：interrupt_on 非 None
```

**关键**：Synapse 的 middleware 位于 SDK base 和 tail 之间。FilesystemMiddleware（base）在最外层处理文件操作，HumanInTheLoopMiddleware（tail）在最内层做审批拦截。这意味着 DescribeImage 在文件操作之前执行（正确——先转图片再读写），Steer 在 retry 之后执行（正确——不给重试注入重复引导）。

### `agent.astream` 降级链

`_iter_stream_events()` 在异步路径尝试 3 种参数组合，同步路径尝试 5 种（stream.py 行 1319-1469）：

```
异步路径（优先）:
  astream(version="v2", stream_mode=["messages","updates"], subgraphs=True)  → 1st
  astream(stream_mode=["messages","updates"], subgraphs=True)                → 2nd
  astream(stream_mode=["messages","updates"])                                → 3rd

同步路径（降级）:
  stream(stream_mode=["messages","updates"], subgraphs=True, version="v2")   → 1st
  stream(stream_mode=["messages","updates"], subgraphs=True)                 → 2nd
  stream(stream_mode=["messages","updates"], version="v2")                   → 3rd
  stream(stream_mode=["messages","updates"])                                 → 4th
  stream(stream_mode=["updates"])                                            → 5th (终极回退)
```

每次 `TypeError` 触发降级，适配不同版本的 LangGraph API。

### 中间件构造的 `_dual_wrap` 元工厂模式

`middleware.py` 中大多数工厂基于两个核心 helper（行 231-274）：

```python
def _dual_wrap_model_call(*, name, apply):
    # 动态构造 AgentMiddleware 子类，注入 wrap_model_call + awrap_model_call
    # apply(request: ModelRequest) -> ModelRequest  （纯函数变换）

def _dual_wrap_tool_call(*, name, apply):
    # 同理，注入 wrap_tool_call + awrap_tool_call
    # apply(request: ToolCallRequest) -> ToolCallRequest
```

`build_path_normalize_middleware`、`build_tool_exclusion_middleware`、`build_memory_injection_middleware`、`build_plan_tracking_middleware` 均基于此模式。`build_intent_schema_middleware` 稍微特殊——它返回 **2 个 middleware 的列表**（一个改造 model schema 注入 `intent` 字段，另一个在 tool call 时剥离 `intent` 字段，防止真实工具 schema 验证失败）。

### `NotifyingModelRetryMiddleware` 精确参数

```python
NotifyingModelRetryMiddleware(
    max_retries=999,                    # 极激进——几乎无限重试
    retry_on=should_retry_transient_model_error,
    on_failure="error",
    initial_delay=1.0, backoff_factor=2.0, max_delay=8.0, jitter=True,
)
```

**`should_retry_transient_model_error` 两阶段判断**（middleware.py 行 102-126）：
1. 有 HTTP status_code → 仅 `429/502/503/504`，且 body 匹配 `_RETRYABLE_5XX_MARKERS`（含 `auth_unavailable` 等特殊标记）
2. 无 status_code → body 匹配 `_TRANSIENT_MODEL_ERROR_MARKERS`（`overloaded`、`rate limit`、`capacity`、`busy` 等）

### `SteerQueue` 线程安全设计

核心约束：**不在锁内调用用户回调**。所有 listener 通知通过快照-入队-锁外消费三步完成：

```
push(text):
  with lock: items.append + 快照(items, listeners) 入队
  lock 外: for callback in listeners: callback(snapshot)

drain():
  with lock: items 全部取出 + 快照入队
  lock 外: for callback in listeners: callback(snapshot)
```

`build_steer_middleware` 使用 `before_model` / `abefore_model` 钩子（非 `wrap_model_call`），在每次 LLM 调用前 `drain()` 队列并将引导内容格式化为 `[Mid-run user guidance]` HumanMessage。多条引导编号列出（`1. ...\n2. ...`）。

### `ToolIgnoreMatcher` gitignore 转换规则

`_gitignore_pattern_to_regex()`（tool_ignore.py 行 39-96）完整映射：

| gitignore 语法 | 正则片段 | 说明 |
|---------------|---------|------|
| `*` | `[^/]*` | 匹配除 `/` 外的任意字符 |
| `**` | `.*` | 匹配任意字符（含 `/`） |
| `**/` | `(?:.*/)?` | 可选任意路径前缀 |
| `?` | `[^/]` | 单字符 |
| `[abc]` / `[!abc]` | 直接映射字符类 | 含否定 |
| 尾 `/`（目录规则） | 去掉，追加 `(?:/.*)?$` | 匹配目录及所有后代 |
| `/` 开头 | `^` 紧随其后 | 锚定到根 |
| 中间含 `/` 无前导 `/` | 自动锚定 | 防止部分路径匹配 |

`is_ignored()` 遍历所有规则，最后匹配的规则生效（否定 `!` 翻转），实现 gitignore 标准的"后覆盖前"语义。

### `reasoning_content` Monkey-Patch 三段式

`llm_openai_compat.py` 对 `langchain_openai` 的三个模块级函数进行全局替换（幂等开关 `_PATCHED`）：

1. **`_convert_delta_to_message_chunk`**（流式）：delta 中的 `reasoning_content` → `AIMessageChunk.additional_kwargs['reasoning_content']`
2. **`_convert_dict_to_message`**（完整消息）：同上逻辑
3. **`_convert_message_to_dict`**（发送方向）：将 prior `reasoning_content` 从 `additional_kwargs` 回写到请求体 —— DeepSeek 要求工具多轮时回传之前的思考内容，否则 400 错误

`deepseek_thinking_kwargs()` 构建 thinking 配置：
```python
# enabled=True  → {"reasoning_effort": "high", "extra_body": {"thinking": {"type": "enabled"}}}
# enabled=False → {"extra_body": {"thinking": {"type": "disabled"}}}
```

### Per-Model AsyncClient 生命周期

每个 OpenAI 模型拥有独立的 `httpx.AsyncClient`（`http_clients.py` 行 113-138）：

- 在 AsyncRuntime 的守护事件循环上通过 `runtime.run(_build())` 创建
- `keepalive_expiry=300s`，`max_connections=1000`，`max_keepalive_connections=100`
- 通过 `_async_api_key()` callable 注入 ChatOpenAI —— 阻止 SDK 用明文 key 创建同步客户端
- 进程共享 SSLContext：`shared_openai_ssl_context()` 单例
- 关闭时先关 WebSocket（若存在），再 `runtime.close_connection(client)` → `aclose()`
- 三个 SDK 级 patcher（`enable_openai_long_keepalive_defaults` / `enable_anthropic_long_keepalive_defaults` / `enable_long_keepalive_http_defaults`）清除 SDK 内部缓存的 httpx client 引用

### WebSocket 模式串行化

`ResponsesWebSocketChatOpenAI._astream_responses()` 使用 `asyncio.Lock` 确保**同一时刻只有一个请求**使用 WebSocket 连接（`llm_openai_websocket.py` 行 119）：

```
async with self._responses_ws_lock:
    connection = await self._ensure_responses_websocket()
    try:
        await connection.send(event)
        while True:
            event = await self._recv_event(connection)
            if event_type in {"response.completed", "response.failed", "response.incomplete"}:
                break
            yield generation_chunk
    except BaseException:
        await self._reset_responses_websocket()  # 销毁连接，下次重建
        raise
```

关键设计：**任何异常**（取消/协议错误/断开）都销毁连接，下次请求自动通过 `_ensure_responses_websocket()` 重建。

### `_make_tool()` MCP 工具 5 步转换

`mcp_client.py` 行 251-276：

```
Step 1: prefix = server.tool_prefix or f"{server.name}__"
Step 2: full_name = f"{prefix}{tool_name}"
Step 3: safe_name = re.sub(r'[^a-zA-Z0-9_-]', '_', full_name)  # 清洗特殊字符
Step 4: args_model = json_schema_to_pydantic_model(safe_name, schema)
        → _json_schema_to_args() 规范化 → _annotation_for_prop() 类型映射
        → pydantic.create_model() 动态创建（extra="allow" 前向兼容）
Step 5: StructuredTool.from_function(func=_invoke, name=safe_name, ...)
```

`_annotation_for_prop()` 递归处理 `array`（`list[T]`）、`anyOf`（`Union[T1, T2]`）、`$ref` 等 JSON Schema 构造。`_invoke()` 闭包捕获 `server_name` 和 `tool_name`，过滤掉显式 `None` 值后调用 `call_tool()`。

### `SessionStore.touch()` COALESCE 语义

`sessions.py` 行 244-248：

```sql
UPDATE sessions SET
    model = COALESCE(?, model),
    active_model = COALESCE(?, active_model),
    thinking = COALESCE(?, thinking),
    updated_at = ?
WHERE thread_id = ?
```

**`COALESCE(new_val, old_val)` 确保 NULL 不会覆盖已有值**。这允许先绑定模型再对话、或切换模型后保持绑定。`is_default_session_title()` (行 64-75) 检测 title 是否为 `thread_id` 自身、`"session *"`、`"untitled"` 等占位符，是则自动替换为首条用户消息的前 80 字符。

### `CheckpointSeeder.seed_messages()` 完整验证

`checkpoint_seed.py` 行 104-144，在写入后执行 7 项验证：

```
1. _validate_thread_id()        # 非空，≤120字符，无NULL字节
2. _validate_messages()         # 仅 HumanMessage/AIMessage，唯一 stable ID，非空 text
3. 目标 thread 必须为空          # saver.get_tuple(config) is not None → 拒绝
4. agent.update_state(..., as_node="model")    # 写入消息
5. agent.update_state(..., as_node=END)        # 密封到 END 节点
6. _verify_terminal():
   - state.next 为空（无 pending tasks）
   - state.interrupts 为空
   - round-trip messages 精确匹配（_messages_match）
   - checkpoint tuple 可读回
7. 失败 → _compensate(): saver.delete_thread(thread_id) 回滚
```

### TUI `_tick_status` 100ms 定时器

`CodingAgentApp.on_mount()` 注册 `set_interval(0.1, self._tick_status)`，每 100ms 执行：

1. **Spinner 动画**：`_spin_i = (_spin_i + 1) % len(_SPIN_CHARS)`，busy 时显示旋转字符
2. **ThoughtBlock live 时钟**：无需新 token 时持续推进 "Thinking… Xs" 计时
3. **Status notice 过期检测**：检查并清除过期的临时通知
4. **SessionRecap 空闲检测**：`SessionRecapController.try_fire()` 判定空闲 180s 触发一行概要

### `_bg_build_agent` 延迟构建机制

`tui_defer_agent=True`（默认）时，`on_mount()` 通过 `@work(thread=True)` 在后台线程构建 agent：

```
Phase 1: build_coding_agent(load_mcp=False)     # 不加载 MCP（快速启动）
Phase 2: attach_mcp_to_agent()                  # 异步加载 MCP tools + rebuild agent
```

`_agent_ready` (threading.Event) 控制 `run_turn()` 等待。Phase 2 复用 Phase 1 的 model/checkpointer/subagents/steer_queue，仅替换 tools 列表，避免重复的模型连接建立。

### `attach_mcp_to_agent` 热重载

当 MCP 配置变化时，不重建 model 和 checkpointer，只替换 tools（agent.py 行 441-467）：

```python
def attach_mcp_to_agent(settings, agent, *, project_root=None):
    # 从 agent._coding_* 属性提取已有组件
    checkpointer = agent._coding_checkpointer
    model = agent._coding_model
    registry = agent._coding_model_registry
    steer_queue = agent._coding_steer_queue

    # 从活跃 MCP pool 中取已加载的 tools
    pool = get_active_mcp_pool()
    pool_tools = list(pool.tools) if pool else None

    return build_coding_agent(
        settings, ..., model=model, checkpointer=checkpointer,
        mcp_tools=pool_tools, steer_queue=steer_queue,
    )
```

这保证了 MCP 重载不会中断当前会话的 checkpoint 连续性。`steer_queue` 跨 rebuild 共享，未消费的引导不丢失。
