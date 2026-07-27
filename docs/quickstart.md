# 快速开始

安装完成后，按以下步骤快速上手。

## 1. 配置 API Key

Synapse 使用 **分层配置** 系统，推荐使用 `models.json` 管理 API Key：

```bash
mkdir .coding-agent
```

创建 `.coding-agent/models.json`：

```json
{
  "default": "gpt-4.1",
  "models": {
    "gpt-4.1": {
      "provider": "openai",
      "model": "gpt-4.1",
      "api_key_env": "OPENAI_API_KEY"
    }
  }
}
```

然后在项目根目录创建 `.env` 文件：

```
OPENAI_API_KEY=sk-your-key-here
```

也可以通过环境变量直接设置：

```bash
# Windows PowerShell
$env:OPENAI_API_KEY="sk-your-key-here"

# Linux / macOS
export OPENAI_API_KEY="sk-your-key-here"
```

## 2. 运行

```bash
# TUI 全屏交互界面（推荐）
synapse tui -w .

# 单次任务执行
synapse run "总结当前项目结构" -w .

# CLI 对话
synapse chat -w .
```

## 3. 基本交互

启动 TUI 后，底部有输入框，直接输入自然语言即可：

- `查看 src/ 目录结构`
- `解释 cli.py 的主要功能`
- `帮我写一个测试文件`
- `/help` — 查看内置 slash 命令

## 4. 常用选项

| 选项 | 说明 |
|---|---|
| `-w / --workspace PATH` | 指定工作目录 |
| `-m / --model NAME` | 选择模型 profile |
| `--require-approval` | 启用手动审批（默认关闭） |
| `--readonly` | 只读模式（禁止写入和执行） |
| `--thread-id ID` | 恢复指定会话 |
| `--debug` | 调试模式 |

## 5. 会话管理

```bash
# 列出最近会话
synapse sessions list

# 导出会话记录
synapse sessions export <thread_id> -f md
```

## 下一步

- [CLI 命令参考](cli.md) — 所有命令的详细说明
- [配置指南](config.md) — 完整的配置项参考
- [模型配置](models.md) — 多模型 / 多 profile 配置
