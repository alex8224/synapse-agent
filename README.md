# Synapse

基于 **LangChain Deep Agents** 的本地 Synapse。

- Harness: `deepagents.create_deep_agent`
- Backend: `LocalShellBackend`（**无 sandbox**）
- 默认审批: **关闭 / 自动通过**
- 依赖管理: `uv`

## 功能

| 能力 | 说明 |
|---|---|
| 读改代码 | `ls/read/write/edit/glob/grep` |
| 执行命令 | `execute` 本地 shell |
| 规划 | `write_todos` |
| 子代理 | 默认 `researcher` / `tester` / `reviewer`（`task` 委派） |
| 自定义工具 | `git_status` / `git_diff` / `run_tests` |
| 记忆 | `AGENTS.md` |
| Skills | `skills/**`（Agent Skills frontmatter） |
| 会话 | sqlite checkpointer + 会话元数据管理 |
| 多模型 | `ModelRegistry`（单模型兼容 / JSON 多 profile） |
| MCP | 配置 MCP Server 后注入为 tools |
| 权限/只读 | `FilesystemPermission` + `HarnessProfile.excluded_tools` |
| CLI | `run` / `chat` / `tui` / `sessions` / `models` / `mcp` / `version` |
| 可选 HITL | `--require-approval`（默认关） |

## 安装

### 前置要求

- Python >= 3.12
- [uv](https://docs.astral.sh/uv/getting-started/installation/)（包管理器）

### 方法一：安装为系统 CLI 工具（推荐）

项目已声明 console script，可一次安装到用户 PATH，之后无需 `uv run`。

```powershell
# 可编辑安装（开发推荐；改代码立刻生效）
powershell -ExecutionPolicy Bypass -File scripts/install.ps1

# 或手动
uv tool install --editable --force .
```

安装后任意目录直接使用：

```bash
synapse tui -w .
synapse run "查看当前仓库结构并总结" -w .
```

仓库根目录还提供 `synapse.cmd` 薄启动器（优先 PATH 上的入口，其次 `.venv\Scripts`，最后回退 `uv run`）。

### 方法二：本地 venv 开发

```bash
# 同步依赖
uv sync

# 使用 venv 入口（Windows）
.\.venv\Scripts\synapse.exe tui -w .

# 或模块入口
uv run python -m synapse tui -w .

# 兼容旧写法
uv run synapse chat -w .
```

### 卸载

```powershell
uv tool uninstall synapse
# 或
powershell -ExecutionPolicy Bypass -File scripts/install.ps1 -Uninstall
```

## 快速开始

安装并配置完成后，即可运行：

```bash
# TUI 交互界面（推荐）
synapse tui -w .

# 单次执行
synapse run "总结当前项目结构" -w .

# CLI 对话
synapse chat -w .
```

### 会话管理

```bash
synapse sessions list
synapse sessions export <thread_id> -f md
# 默认写入 .coding-agent/exports/<thread_id>.md；打印到终端加 --stdout
```

### 模型与 MCP 管理

```bash
synapse models list
synapse mcp list
```

## 配置

Synapse 采用**分层配置**策略：用户全局配置（`~/.synapse/`）与项目本地配置（`<workspace>/.synapse/`）合并，项目层覆盖用户层。

```
~/.synapse/              # 用户全局层（优先级低）
  models.json            # 模型 profiles + api_key（推荐方式）
  mcp.json               # MCP Server 定义
  settings.json          # 非敏感 Settings 覆盖
  themes.json            # 自定义 UI 主题

<workspace>/.synapse/    # 项目层（优先级高，覆盖用户层）
  models.json
  mcp.json
  settings.json
  themes.json
  system_prompt.md       # 自定义系统提示
  sessions.sqlite
  checkpoints.sqlite
```

### 方式一：环境变量（.env 快速入门）

从模板创建，填入密钥即可：

```bash
cp .env.example .env
```

核心变量：

| 变量 | 必填 | 说明 |
|---|---|---|
| `MODEL` | 是 | 模型标识，如 `openai:gpt-4.1`、`openai:deepseek-chat` |
| `OPENAI_API_KEY` | 是* | OpenAI 兼容 API 密钥 |
| `OPENAI_BASE_URL` | 否 | 自定义 API 端点（中转/本地服务需填） |
| `ANTHROPIC_API_KEY` | 否 | Anthropic 原生 API 密钥 |
| `WORKSPACE` | 否 | 工作区路径，默认 `.` |
| `SHELL_EXECUTABLE` | 否 | Shell 类型，默认 `pwsh`（可选 `cmd`/`bash`） |
| `SHELL_TIMEOUT` | 否 | 命令超时秒数，默认 120 |
| `TOKEN_STREAM` | 否 | token 级流式输出，默认 `true` |
| `PARALLEL_TOOL_CALLS` | 否 | 并发工具调用，默认 `true` |

完整变量列表见 `.env.example`。

### 方式二：models.json（多模型 profiles，推荐）

在 `~/.synapse/` 或 `<workspace>/.synapse/` 下创建 `models.json`。支持多 profile、自定义模型参数（temperature、max_tokens、thinking 等）。

参考示例：`examples/models.example.json`

```json
{
  "default": "primary",
  "models": {
    "primary": {
      "model": "openai:gpt-4.1",
      "api_key": "sk-REPLACE_ME",
      "context_window": 128000,
      "temperature": 0.2,
      "max_tokens": 8192
    },
    "deepseek": {
      "model": "openai:deepseek-v4-pro",
      "api_key": "sk-REPLACE_ME",
      "base_url": "http://127.0.0.1:3000/v1",
      "context_window": 128000,
      "thinking": "high",
      "temperature": 0.2
    }
  }
}
```

通过 `AGENT_ACTIVE_MODEL` 环境变量或 CLI 参数切换 profile。

### MCP 服务器配置

在 `~/.synapse/` 或 `<workspace>/.synapse/` 下创建 `mcp.json`。

参考示例：`examples/mcp.example.json`

```json
{
  "servers": [
    {
      "name": "filesystem",
      "transport": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "."],
      "enabled": false
    },
    {
      "name": "anysearch",
      "transport": "streamable_http",
      "url": "https://api.anysearch.com/mcp",
      "headers": {
        "Authorization": "Bearer ${ANYSEARCH_API_KEY}"
      },
      "enabled": false,
      "tool_prefix": "anysearch__"
    }
  ]
}
```

配置后通过 `synapse mcp list` 查看状态，可在 TUI 中动态启用/禁用。

### Settings 覆盖

在 `~/.synapse/settings.json` 或 `<workspace>/.synapse/settings.json` 中写入非敏感配置，与 `.env` 环境变量作用相同但优先级更高（项目层 > 用户层 > 环境变量）。

## 使用技巧

### TUI 快捷键

| 快捷键 | 功能 |
|--------|------|
| `Ctrl+T` | 折叠/展开最近工具组 |
| `Ctrl+E` | 展开/收起最近 Thought 摘要 |
| `Ctrl+L` | 清空 transcript |
| `Alt+C` | 复制当前划选（无选区时复制最近答案） |
| `Ctrl+Shift+Y` | 复制最近一条助手答案 |
| `Ctrl+C` / `Ctrl+Q` | 退出 |

文本选择：transcript 中答案/Thought/工具组/用户条支持鼠标拖选（有高亮），划选后用 `Alt+C` 复制。

### 斜杠命令（chat / TUI）

- `/help` `/thread` `/new` `/sessions` `/session ...` `/switch <id>`
- `/rename` `/export [md\|json] [path]`
- `/model` `/model <alias>`
- `/mcp list|tools|test|reload|enable|disable|config`
- `/clear` `/exit`

补全：TUI 输入 `/` 后有 ghost 建议（`Tab`/`→` 接受，`Shift+Tab` 上一项，`Ctrl+Space` 列出候选）；chat 模式下 `Tab` 补全。

### MCP transports

| transport | 用途 | 关键字段 |
|-----------|------|----------|
| `stdio` | 本地 MCP 进程 | `command` / `args` / `env` |
| `sse` | 远程 SSE | `url` / `headers` |
| `streamable_http` / `http` | 远程 Streamable HTTP | `url` / `headers` |

连接在后台事件循环中保持复用；`/mcp reload` 重建 agent 与连接池。

### models.json 字段说明

| 字段 | 含义 |
|---|---|
| `api_key` | **推荐**：密钥直接写在 models.json |
| `api_key_env` | 旧方式：从环境变量读密钥 |
| `thinking` / `thinking_level` / `reasoning_effort` | 思考级别：`off\|minimal\|low\|medium\|high\|max` |
| `enable_thinking` | 兼容旧字段 bool |
| `temperature` / `max_tokens` / `timeout` 等 | 直接传给 ChatModel |
| `stream_chunk_timeout` | 流式相邻 chunk 静默超时（秒）；默认关闭，避免长思考被 langchain-openai 120s 掐断 |
| `model_kwargs` | 请求体 kwargs |
| `extra_body` | 厂商扩展体（与 thinking 合并） |

`.env` 仍可读，仅作迁移/CI 兼容，**不推荐**作为常规分发方式。

## 示例：修复 sample_repo

`tests/fixtures/sample_repo` 中 `sub()` 有意写错，可用于验证 agent 闭环：

```bash
# 先确认测试失败
uv run pytest tests/fixtures/sample_repo -q

# 让 agent 修复（需要有效模型密钥）
uv run synapse run "修复 calculator.sub 的 bug，使 tests 全部通过" -w tests/fixtures/sample_repo
```

## 环境变量参考

关键 Agent 行为变量：

| 变量 | 默认 | 含义 |
|---|---|---|
| `MODEL` | `openai:gpt-4.1` | `provider:model` 或 profile 名 |
| `AGENT_MODELS_CONFIG` | - | 多模型 JSON 路径 |
| `AGENT_ACTIVE_MODEL` | - | 当前 profile 别名 |
| `WORKSPACE` | `.` | 工作区 |
| `AGENT_REQUIRE_APPROVAL` | `false` | 是否启用 HITL |
| `AGENT_ENABLE_SUBAGENTS` | `true` | 默认子代理 |
| `AGENT_READONLY` | `false` | 排除写/执行工具 |
| `AGENT_ENABLE_FS_PERMISSIONS` | `false` | 文件系统权限规则 |
| `AGENT_ENABLE_MCP` | `true` | 启用 MCP 注入 |
| `AGENT_MCP_EAGER` | `false` | 启动时立刻连 MCP（默认延迟到 TUI 后台二阶段） |
| `AGENT_TUI_DEFER_AGENT` | `true` | TUI 先起 UI，后台 build agent |
| `AGENT_MCP_CONFIG` | - | MCP servers JSON |
| `CHECKPOINT_BACKEND` | `sqlite` | `sqlite` 或 `memory` |
| `INHERIT_ENV` | `true` | shell 继承主机环境 |
| `VIRTUAL_MODE` | `true` | 文件路径虚拟根 |

## 安全说明

- **不使用 sandbox**：命令在宿主机执行。
- 默认**不审批**；仅建议在受信开发机使用。
- 可用 `--require-approval` 临时打开 HITL。
- `safety.py` 提供危险命令黑名单（警告/检测用）；默认不拦截 agent 内置 `execute`。
- `--readonly` / `AGENT_READONLY=true` 通过 harness 排除 `execute/write_file/edit_file`。

## 开发

```bash
uv run pytest
uv run ruff check src tests
```

## 项目结构

```text
src/synapse/
  agent.py           # create_deep_agent 装配
  backends.py        # LocalShellBackend
  cli.py             # typer CLI
  config.py          # pydantic-settings
  models_registry.py # 多模型目录
  mcp_client.py      # MCP → tools
  sessions.py        # 会话元数据
  subagents.py       # 默认子代理
  harness.py         # excluded_tools
  fs_permissions.py  # FilesystemPermission
  prompts.py
  safety.py
  tools/             # git / run_tests
  ui/                # stream + TUI
docs/design.md
skills/              # agent skills
examples/            # models/mcp 配置样例
AGENTS.md
```

## 设计文档

更完整的架构与分期说明见 [`docs/design.md`](docs/design.md)。
