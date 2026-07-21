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

## 快速开始

### 推荐：安装为单一入口（不必每次 `uv run`）

项目已声明 console script：

```toml
[project.scripts]
synapse = "synapse.cli:main"
```

一次安装到用户 PATH：

```powershell
# 可编辑安装（开发推荐；改代码立刻生效）
powershell -ExecutionPolicy Bypass -File scripts/install.ps1

# 或手动
uv tool install --editable --force .
```

之后任意目录直接：

```bash
synapse version
synapse tui -w .
synapse run "查看当前仓库结构并总结" -w .
synapse sessions list
```

卸载：

```powershell
uv tool uninstall synapse
# 或
powershell -ExecutionPolicy Bypass -File scripts/install.ps1 -Uninstall
```

仓库根目录还提供 `synapse.cmd` 薄启动器（优先 PATH 上的入口，其次 `.venv\Scripts`，最后回退 `uv run`）。

### 本地开发（仍可用 venv）

```bash
# 1) 同步依赖
uv sync

# 2) 配置密钥
# 编辑 .env 填入 OPENAI_API_KEY 或 ANTHROPIC_API_KEY

# 3) 使用 venv 里的入口（Windows）
.\.venv\Scripts\synapse.exe tui -w .

# 4) 或模块入口
uv run python -m synapse tui -w .

# 5) 兼容旧写法
uv run synapse chat -w .
```

也可用：

```bash
uv run python -m synapse run "..."
```

### 会话 / 模型 / MCP

```bash
uv run synapse sessions list
uv run synapse sessions export <thread_id> -f md
uv run synapse models list
uv run synapse mcp list
uv run synapse mcp test
```

chat / tui 斜杠命令：

- `/help` `/thread` `/new` `/sessions` `/session ...` `/switch <id>`
- `/rename` `/export [md|json] [path]`
- `/model` `/model <alias>`
- `/mcp list|tools|test|reload|enable|disable|config`
- `/clear` `/exit`

补全：

- TUI：输入 `/` 后有 ghost 建议；`Tab`/`→` 接受，`Shift+Tab` 上一项，`Ctrl+Space` 列出候选
- chat：readline/`pyreadline3` 下 `Tab` 补全 slash 命令

TUI 快捷键：
- `Ctrl+T` 折叠/展开最近工具组（历史 turn 工具组也支持）
- `Ctrl+E` 展开/收起最近 Thought 摘要
- `Ctrl+L` 清空 transcript
- `Ctrl+C` / `Ctrl+Q` 退出

### MCP transports

配置见 `examples/mcp.example.json`：

| transport | 用途 | 关键字段 |
|-----------|------|----------|
| `stdio` | 本地 MCP 进程 | `command` / `args` / `env` |
| `sse` | 远程 SSE | `url` / `headers` |
| `streamable_http` / `http` | 远程 Streamable HTTP | `url` / `headers` |

连接会在后台事件循环中保持复用；`/mcp reload` 重建 agent 与连接池。

### 多模型配置（推荐）

模型 id / base_url / 思考级别 / **api_key** 写在配置文件。优先不再依赖 `.env`。

分层目录（后层覆盖前层，同名 profile 字段合并，密钥可继承）：

1. 用户全局：`~/.synapse/`
2. 便携包（可选）：`<exe 同级>/.synapse/`
3. 项目：`<workspace>/.synapse/`

```text
~/.synapse/
  models.json      # 全局模型 + api_key
  mcp.json
  settings.json

my-project/.synapse/
  models.json      # 项目覆盖 / 增补
  mcp.json
  settings.json
  sessions.sqlite
  checkpoints.sqlite
```

默认路径也可由 `AGENT_MODELS_CONFIG` 指定单文件（不再分层合并）。

```json
{
  "default": "primary",
  "models": {
    "primary": {
      "model": "openai:deepseek-v4-pro",
      "api_key": "sk-...",
      "base_url": "http://127.0.0.1:3000/v1",
      "thinking_level": "high",
      "temperature": 0.2,
      "max_tokens": 8192
    }
  }
}
```

字段说明：

| 字段 | 含义 |
|---|---|
| `api_key` | **推荐**：密钥直接写在 models.json |
| `api_key_env` | 旧方式：从环境变量读密钥 |
| `thinking` / `thinking_level` / `reasoning_effort` | 思考级别：`off\|minimal\|low\|medium\|high\|max` |
| `enable_thinking` | 兼容旧字段 bool |
| `temperature` / `max_tokens` / `timeout` 等 | 直接传给 ChatModel |
| `stream_chunk_timeout` | 流式相邻 chunk 静默超时（秒）；默认由 settings 关闭，避免长思考被 langchain-openai 120s 掐断 |
| `model_kwargs` | 请求体 kwargs |
| `extra_body` | 厂商扩展体（与 thinking 合并） |

`settings.json` 可放非密钥运行参数（见 `examples/settings.example.json`；其中 `stream_chunk_timeout` 默认 `null` 关闭静默超时，需要时可设为正数秒）。  
`.env` 仍可读，仅作迁移/CI 兼容，**不推荐**作为常规分发方式。

```bash
uv run synapse models list
# TUI/chat: /model  /model primary  /model thinking high
```

样例：`examples/models.example.json`、`examples/settings.example.json`。

### MCP 配置示例

分层：`~/.synapse/mcp.json` + `<project>/.synapse/mcp.json`（同名 server 后层覆盖）。  
也可 `AGENT_MCP_CONFIG` 指定单文件。见 `examples/mcp.example.json`。

## 示例：修复 sample_repo

`tests/fixtures/sample_repo` 中 `sub()` 有意写错，可用于验证 agent 闭环：

```bash
# 先确认测试失败
uv run pytest tests/fixtures/sample_repo -q

# 让 agent 修复（需要有效模型密钥）
uv run synapse run "修复 calculator.sub 的 bug，使 tests 全部通过" -w tests/fixtures/sample_repo
```

## 配置

关键项：

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
