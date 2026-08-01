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

### 子代理

| 变量 | 默认值 | 说明 |
|---|---|---|
| `AGENT_ENABLE_SUBAGENTS` | `true` | 启用子代理 |
| `AGENT_SUBAGENT_TESTER_MODEL` | — | Tester 子代理模型 |
| `AGENT_SUBAGENT_REVIEWER_MODEL` | — | Reviewer 子代理模型 |

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
| `AGENT_EXPAND_THINKING` | `false` | TUI 思考块结束后默认展开完整推理内容（默认折叠为单行预览，点击或 `ctrl+t` 仍可切换） |
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
