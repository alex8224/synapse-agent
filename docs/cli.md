# CLI 命令参考

Synapse 使用 [Typer](https://typer.tiangolo.com/) 构建 CLI。

## 命令总览

```
synapse                    # 默认启动 TUI
synapse tui                # 启动 TUI
synapse run "..."          # 单次执行
synapse chat               # CLI 对话

synapse sessions list      # 列出会话
synapse sessions codex-list     # 列出 Codex 会话
synapse sessions codex-inspect  # 查看 Codex 会话元信息
synapse sessions codex-preview  # 预览 Codex 会话内容
synapse sessions codex-import   # 导入 Codex 会话

synapse models list        # 列出模型 profile
synapse models set         # 切换活跃模型

synapse mcp list           # 列出 MCP Server
synapse mcp tools          # 查看 MCP 工具
synapse mcp inspect        # 检查 MCP 配置

synapse version            # 显示版本
```

## `synapse tui`

启动全屏 Textual TUI 界面（默认命令）。

```bash
synapse tui [OPTIONS]
```

| 选项 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `-w, --workspace PATH` | Path | `$PWD` | 工作目录 |
| `-m, --model NAME` | str | — | 模型 profile 别名或 `provider:model` |
| `--require-approval` | flag | off | 启用手动审批 |
| `--readonly` | flag | off | 只读模式 |
| `--thread-id ID` | str | — | 恢复指定会话 |
| `--debug` | flag | off | 调试模式 |

## `synapse run`

单次执行任务，完成后退出。

```bash
synapse run [OPTIONS] PROMPT
```

| 参数 | 说明 |
|---|---|
| `PROMPT` | 任务描述（必填） |

选项同 `tui`。

## `synapse chat`

CLI 对话模式，在终端中进行多轮对话。

```bash
synapse chat [OPTIONS]
```

选项同 `tui`。

## `synapse sessions`

会话管理子命令组。

### `synapse sessions list`

列出最近的非空会话。

```bash
synapse sessions list [OPTIONS]
```

| 选项 | 说明 |
|---|---|
| `-n, --limit INT` | 最大返回数（默认 50） |
| `--all` | 包含空占位会话 |

### Codex 相关子命令

用于查看和导入 [Codex](https://github.com/openai/codex) 的历史会话。

```bash
synapse sessions codex-list [-w WORKSPACE] [--codex-home PATH] [-n LIMIT]
synapse sessions codex-inspect NATIVE_ID [-w WORKSPACE] [--codex-home PATH]
synapse sessions codex-preview NATIVE_ID [-w WORKSPACE] [--codex-home PATH] [-n LIMIT] [--offset N]
synapse sessions codex-import NATIVE_ID [-w WORKSPACE] [--codex-home PATH]
```

## `synapse models`

模型 profile 管理。

```bash
synapse models list              # 列出所有可用模型 profile
synapse models set ALIAS         # 设置当前活跃模型
```

## `synapse mcp`

MCP Server 检查和调试。

```bash
synapse mcp list                 # 列出配置的 MCP Server
synapse mcp tools                # 显示 MCP 提供的工具
synapse mcp inspect NAME         # 查看指定 Server 的详细配置
```

## `synapse version`

显示当前版本号。

```bash
synapse version
```
