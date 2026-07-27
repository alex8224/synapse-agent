# Synapse 使用与配置指南

**Synapse** 是一个基于 [LangChain Deep Agents](https://github.com/langchain-ai/deepagents) 的本地编码 Agent。

- **后端**: `LocalShellBackend`（直接执行本地命令，无 sandbox 隔离）
- **默认审批**: 关闭（自动放行，即 `auto_approve=True`）
- **依赖管理**: `uv`
- **Python**: >= 3.12

## 功能概览

| 能力 | 说明 |
|---|---|
| 代码读写 | `read / write / edit / glob / grep` 等文件工具 |
| 命令执行 | `execute` 本地 shell（支持 pwsh / bash / cmd） |
| 任务规划 | `write_todos` 任务拆解与追踪 |
| 子代理 | `researcher` / `tester` / `reviewer`（通过 `task` 委派） |
| 记忆 | `AGENTS.md` / `MEMORY.md` 项目级记忆持久化 |
| Skills | `skills/**` Agent Skills 插件体系 |
| 会话 | SQLite checkpointer + 会话元数据管理 |
| 多模型 | `ModelRegistry` — 支持单模型 / JSON 多 profile 切换 |
| MCP | 配置 MCP Server 后自动注入为 tools |
| 权限控制 | `FilesystemPermission` + `HarnessProfile.excluded_tools` + 只读模式 |
| CLI | `tui` / `run` / `chat` / `sessions` / `models` / `mcp` / `version` |
| HITL | 可选人工审批（`--require-approval`，默认关闭） |

## 界面

| 命令 | 说明 |
|---|---|
| `synapse tui -w .` | 全屏 Textual TUI（推荐） |
| `synapse run "任务描述" -w .` | 单次执行 |
| `synapse chat -w .` | CLI 对话模式 |

## 项目结构

```
.
├── AGENTS.md              # Agent 记忆 / 行为约定
├── pyproject.toml         # 项目元信息与依赖
├── mkdocs.yml             # 文档站点配置
├── skills/                # Agent Skills 插件
├── src/synapse/           # 主代码
│   ├── cli.py             # CLI 入口（Typer）
│   ├── agent.py           # Agent 构建
│   ├── config.py          # 配置系统（Pydantic Settings）
│   ├── models_registry.py # 多模型注册表
│   ├── mcp_client.py      # MCP 客户端
│   ├── sessions.py        # 会话管理
│   ├── harness.py         # 工具 / 权限编排
│   └── ui/                # Textual TUI 界面
├── tests/                 # 测试
└── docs/                  # 本文档
```
