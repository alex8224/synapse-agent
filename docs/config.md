# 配置指南

Synapse 使用 **Pydantic Settings** 实现分层配置系统。

## 配置加载顺序

优先级从高到低：

1. **CLI 参数**（如 `-m gpt-4.1`、`--readonly`）
2. **环境变量**（`OPENAI_API_KEY` 等）
3. **`.env` 文件**（项目根目录，覆盖系统环境变量）
4. **分层 JSON 配置**（`models.json`、`mcp_servers.json`）
5. **代码默认值**

## 配置文件路径

| 用途 | 路径 |
|---|---|
| 模型配置 | `.coding-agent/models.json` |
| MCP 配置 | `.coding-agent/mcp_servers.json` |
| 主题自定义 | `.coding-agent/themes.json` |
| 环境变量 | 项目根目录 `.env`（legacy，推荐 models.json `api_key_env`） |
| 会话数据 | `.coding-agent/checkpoints.sqlite` |

## 环境变量参考

### 模型相关

| 变量 | 默认值 | 说明 |
|---|---|---|
| `MODEL` | `openai:gpt-4.1` | 默认模型（legacy 单模型模式） |
| `OPENAI_API_KEY` | — | OpenAI API Key |
| `OPENAI_BASE_URL` | — | OpenAI API 自定义网关 |
| `OPENAI_WEBSOCKET` | `false` | 启用 WebSocket 连接；瞬时断流按模型 `max_retries` 重连，耗尽后在尚未输出内容时回退 HTTP/SSE |
| `OPENAI_FAST_MODE` | `false` | Codex Fast 档：对 `auth=openai_oauth` 的模型请求注入 `service_tier=priority`（优先处理、计费更高）。可用 `/fast on\|off\|status` 运行时切换；详见 [models.md](models.md) 的「Codex Fast 档」 |
| `ANTHROPIC_API_KEY` | — | Anthropic API Key |
| `AGENT_MODELS_CONFIG` | — | `models.json` 路径（覆盖默认） |
| `MODELS_JSON` | — | 内联 JSON（替代文件） |
| `AGENT_ACTIVE_MODEL` | — | 活跃模型 profile 别名 |
| `VISION_MODEL` | — | 独立视觉模型的 OpenAI 兼容配置 |

### 工作区

| 变量 | 默认值 | 说明 |
|---|---|---|
| `WORKSPACE` | `$PWD` | 工作目录 |
| `SHELL_TIMEOUT` | `120` | Shell 命令超时（秒） |
| `MAX_OUTPUT_BYTES` | `100000` | 命令输出最大字节数 |
| `SHELL_EXECUTABLE` | `pwsh` | Shell 类型（`pwsh`/`powershell`/`cmd`/`bash`/`system`） |
| `SHELL_ENCODING` | `utf-8` | Shell 输出编码 |
| `INHERIT_ENV` | `true` | 继承系统环境变量 |
| `VIRTUAL_MODE` | `true` | 虚拟文件系统模式 |

### 审批与安全

| 变量 | 默认值 | 说明 |
|---|---|---|
| `AGENT_REQUIRE_APPROVAL` | `false` | 启用人工审批 |
| `AGENT_AUTO_APPROVE` | `true` | 自动放行（审批关闭） |
| `AGENT_SAFETY_PROFILE` | `dev-autopass` | 安全策略 |
| `AGENT_READONLY` | `false` | 全局只读模式 |
| `AGENT_DENY_FS_PATHS` | — | 禁止访问的文件路径（JSON 数组） |
| `AGENT_ENABLE_FS_PERMISSIONS` | `false` | 启用文件系统权限 |
| `AGENT_EXCLUDED_TOOLS` | — | 排除的工具列表（JSON 数组） |
| `ENABLE_COMMAND_BLACKLIST` | `true` | 启用命令黑名单 |

### 会话

| 变量 | 默认值 | 说明 |
|---|---|---|
| `CHECKPOINT_BACKEND` | `sqlite` | 检查点后端（`sqlite`/`memory`） |
| `CHECKPOINT_PATH` | `.coding-agent/checkpoints.sqlite` | 检查点存储路径 |
| `SESSIONS_PATH` | — | 会话元数据路径 |
| `AGENT_PROJECT_CATALOG_ENABLED` | `true` | 启用用户层全局项目目录（`~/.synapse/catalog.sqlite`）。开启后每次启动 TUI 会注册当前项目并投影会话元数据，供跨项目会话列表/搜索使用 |
| `PROJECT_CATALOG_PATH` | `~/.synapse/catalog.sqlite` | 全局项目目录数据库路径（默认用户层，可覆盖为任意路径） |
| `SESSION_SUMMARY_MODE` | `local` | 会话摘要模式：`off` 关闭；`local` 在每轮结束后生成确定性本地摘要（不调用模型）。LLM 摘要为未来扩展位 |
| `SESSION_SUMMARY_MAX_CHARS` | `600` | 本地会话摘要的最大字符数（含多轮条目，超出时从最旧条目开始裁剪） |
| `AGENT_HISTORY_TAIL_TURNS` | `20` | TUI 从轻量 transcript 投影初始读取/渲染的最近可见会话轮数；滚动到顶部后按 turn 游标加载更早历史，最多挂载 5 页 |
| `AGENT_SESSION_PREWARM_ENABLED` | `false` | 恢复大上下文会话后，在后台对该会话历史发起一次最小模型请求，让 provider 预填充并缓存历史前缀，用户第一条消息可命中缓存、大幅缩短首 token 等待。注意：预热本身会按输入 token 计费一次，仅在需要时开启 |
| `AGENT_ENABLE_TOOL_RESPONSE_TRUNCATE` | `false` | 启用工具响应裁剪：keep 窗口（默认最近 40K tokens）外的 `execute`/`read_file`/`search_files` 输出折叠为摘要，减小每轮请求体积（大上下文会话可省 20-30%）。请求层变换，不修改历史 |
| `AGENT_TOOL_RESPONSE_TRUNCATE_KEEP_TOKENS` | `40000` | 工具响应裁剪的 keep 窗口（token），窗口内消息不裁剪 |
| `AGENT_TOOL_RESPONSE_TRUNCATE_MAX_HEAD_CHARS` | `2000` | 关闭 fold 模式时，超长输出保留的头部字符数 |
| `AGENT_TOOL_RESPONSE_TRUNCATE_MAX_TAIL_CHARS` | `0` | 关闭 fold 模式时，超长输出额外保留的尾部字符数（>0 表示 head+tail） |
| `AGENT_TOOL_RESPONSE_TRUNCATE_FOLD_ENABLED` | `true` | fold 模式：窗口外输出一律折叠（有 `tool-output://` 引用的可经 `read_tool_result` 找回原文，无引用的保留头部锚点） |
| `AGENT_TOOL_RESPONSE_TRUNCATE_FOLD_HEAD_CHARS` | `300` | fold 模式下无引用输出保留的头部锚点字符数 |
| `AGENT_TOOL_RESPONSE_TRUNCATE_TOOLS` | `["execute","read_file","search_files"]` | 参与折叠/裁剪的工具名单（JSON 数组） |

### 长程目标（Goals）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `AGENT_ENABLE_GOALS` | `true` | 启用长程目标：给 Agent 注入 `get_goal`/`create_goal`/`update_goal` 工具并统计目标 token/时间用量。关闭后不做记账、不注入工具 |
| `AGENT_GOAL_AUTO_CONTINUE` | `true` | 回合结束后若目标仍为 active，自动开启下一回合继续推进（长程执行核心；用户取消回合或输入新消息时不触发） |

用法：`/goal <objective>` 设置目标（`/goal` 查看摘要，`/goal pause|resume|clear|edit` 管理，`gooooal` 为别名）。目标跨回合持久化；预算耗尽自动置为 budget-limited 并停止自动续跑。运行中按 Esc 会中止当前回合并把 active 目标置为 paused，可用 `/goal resume` 恢复。

### 子代理

| 变量 | 默认值 | 说明 |
|---|---|---|
| `AGENT_ENABLE_SUBAGENTS` | `true` | 启用子代理 |
| `AGENT_SUBAGENT_TESTER_MODEL` | — | Tester 子代理模型 |
| `AGENT_SUBAGENT_REVIEWER_MODEL` | — | Reviewer 子代理模型 |
| `AGENT_SUBAGENT_RESEARCHER_MODEL` | — | Researcher 子代理模型 |
| `AGENT_ENABLE_CUSTOM_SUBAGENTS` | `true` | 加载用户自定义子代理（`.synapse/agents/*.md`） |
| `AGENT_CUSTOM_AGENTS_DIRS` | `[]` | 额外扫描的子代理定义目录（JSON 数组，绝对路径或相对 workspace） |
| `AGENT_DISABLE_BUILTIN_SUBAGENTS` | `[]` | 禁用的内置子代理名（JSON 数组，如 `["tester"]`） |
| `AGENT_SUBAGENT_DEFAULT_MODEL` | — | 所有子代理的全局默认模型；未配置时继承主 Agent 模型 |
| `AGENT_SUBAGENT_DEFAULT_REASONING_EFFORT` | — | 所有子代理的全局默认推理级别（`off`/`minimal`/`low`/`medium`/`high`/`max`） |
| `AGENT_SUBAGENT_MODEL_OVERRIDES_JSON` | `{}` | 按子代理名覆盖模型（JSON 对象，如 `{"tester":"algo:1"}`） |
| `AGENT_SUBAGENT_REASONING_EFFORT_OVERRIDES_JSON` | `{}` | 按子代理名覆盖推理级别（JSON 对象） |

#### 自定义子代理

在用户层 `~/.synapse/agents/*.md` 或项目层 `<workspace>/.synapse/agents/*.md` 放置
Markdown 文件即可新增子代理（项目层覆盖用户层同名定义）。YAML frontmatter 提供元数据，
文件正文是子代理的 system prompt：

```markdown
---
name: security-reviewer
description: Use after security-sensitive changes. Reviews for injection and secret leaks.
model: inherit            # 或 "provider:model-name"
reasoning_effort: high    # 可选：off/minimal/low/medium/high/max；缺省继承主 Agent
tools: [read_file, search_files, find_files, execute]   # 可选 allowlist；省略则继承 find_files/search_files
disallowed_tools: [write_file, edit_file]               # 可选 denylist
ownership: task           # 预留字段，仅支持 task
---

You are a security reviewer. Inspect diffs for...
```

- `name` 与内置（researcher/tester/reviewer）同名时覆盖内置定义。
- `tools: []` 表示仅使用 deepagents 内置工具（不继承主代理工具）。
- 解析失败的文件会被跳过并记录 warning，不会导致启动失败。
- `ownership` / `output_schema` 为未来 handoff 与 workflow 编排预留，当前仅支持 `task`。
- 首次启动会在 `~/.synapse/agents/` 生成 `researcher.md` / `tester.md` / `reviewer.md`
  种子文件，编辑即可覆盖内置；已编辑内容不会被覆盖。同名文件存在但未显式写 `model`
  时，`AGENT_SUBAGENT_*_MODEL` 仍会注入；在文件里显式写 `model`（或 `model: inherit`）
  后由文件接管。

### MCP

| 变量 | 默认值 | 说明 |
|---|---|---|
| `AGENT_ENABLE_MCP` | `true` | 启用 MCP 支持 |
| `AGENT_MCP_CONFIG` | — | MCP 配置文件路径 |
| `AGENT_MCP_EAGER` | `false` | Agent 构建时即时连接 MCP |
| `MCP_SERVERS_JSON` | — | 内联 MCP 配置（JSON） |

### 界面

| 变量 | 默认值 | 说明 |
|---|---|---|
| `AGENT_THEME` | `cursor-dark` | TUI 主题 |
| `AGENT_TOOL_DETAILS_EXPANDED` | `true` | 工具详情默认展开 |
| `AGENT_EXPAND_THINKING` | `false` | 推理块不自动展开：流式时仅显示状态行，结束后折叠为一行预览；设为 `true` 时流式与结束后均完整展开 |
| `AGENT_DEBUG` | `false` | 调试模式 |

### 其他

| 变量 | 默认值 | 说明 |
|---|---|---|
| `AGENT_ENABLE_COMPACT_TOOL` | `true` | 启用对话压缩 |
| `TOKEN_STREAM` | `true` | 启用 token 流式输出 |
| `PARALLEL_TOOL_CALLS` | `true` | 启用并行工具调用 |
| `MAX_CONCURRENCY` | `8` | 最大并行度 |
| `STREAM_CHUNK_TIMEOUT` | — | 流式块超时（秒，None=禁用） |
| `AGENT_SHOW_REASONING_PLACEHOLDERS` | `true` | 网关仅返回推理 token 数、不暴露推理文本时，是否显示占位思考节点；设为 `false` 可隐藏 Codex 等加密推理占位 |
| `LANGSMITH_TRACING` | `false` | 启用 LangSmith 追踪 |
| `LANGSMITH_API_KEY` | — | LangSmith API Key |
| `LANGSMITH_PROJECT` | `coding-agent` | LangSmith 项目名 |

## `.coding-agent/models.json` 格式

参见 [模型配置](models.md) 页面。

### 配置错误提示

启动时如果 `models.json`、`settings.json` 或内联 JSON 环境变量格式错误，Synapse 会输出
简短的错误说明和修复提示后退出，不会显示完整 Python traceback。对于 `models.json`，错误信息会
包含出错文件路径和 JSON 的行列位置；修复配置后重新启动即可。

## `.coding-agent/mcp_servers.json` 格式

参见 [MCP Server](mcp.md) 页面。

## `themes.json` Markdown 样式

`themes.json` 支持在继承现有主题的基础上覆盖 Rich Markdown 元素的样式。主题配置仍按用户层到项目层合并，项目层可以只覆盖部分字段：

```json
{
  "themes": {
    "my-dark": {
      "extends": "cursor-dark",
      "label": "My Dark",
      "markdown": {
        "h1": "bold #ff9e64",
        "h2": "bold #e0af68",
        "paragraph": "#c0caf5",
        "code": "bold #7dcfff",
        "block_quote": "italic #565f89",
        "link_url": "underline #7aa2f7",
        "table.border": "#414868",
        "table.header": "bold #bb9af7"
      }
    }
  }
}
```

支持的 Markdown 键包括：`h1`～`h6`、`paragraph`、`strong`、`em`、`s`、`code`、
`code_block`、`block_quote`、`item`、`link`、`link_url`、`kbd`、`hr`、
`table.border` 和 `table.header`。值使用 Rich style 语法；非法样式和未知键会被忽略，
不会阻止其他主题加载。

`code` 控制行内代码的样式；代码围栏内部的语法高亮仍由主题的 `code_theme`（Pygments
主题名）控制。Markdown 样式在渲染期间局部应用，不会污染其他 Rich 输出。
