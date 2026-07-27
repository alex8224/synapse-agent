# 会话管理

Synapse 使用 SQLite 存储会话检查点（checkpoint）和元数据。

## 会话存储

| 项目 | 默认路径 |
|---|---|
| 检查点数据库 | `.coding-agent/checkpoints.sqlite` |
| 会话元数据 | 与检查点同库 |

## 列出会话

```bash
synapse sessions list

# 限制数量
synapse sessions list -n 20

# 包含空会话（占位）
synapse sessions list --all
```

输出包含：thread_id、标题、模型、时间、消息数等信息。

## 恢复会话

通过 `--thread-id` 选项恢复之前的会话：

```bash
synapse tui --thread-id <thread_id> -w .
synapse run "继续刚才的工作" --thread-id <thread_id> -w .
```

TUI 模式下也可以在界面内浏览和切换历史会话。

## 导出会话

将对话记录导出为 Markdown：

```bash
synapse sessions export <thread_id> -f md
# 默认导出到 .coding-agent/exports/<thread_id>.md

# 加 --stdout 直接输出到终端
synapse sessions export <thread_id> -f md --stdout
```

## Codex 会话导入

支持查看和导入 OpenAI Codex 的历史会话记录。

### 扫描 Codex 会话

```bash
# 列出所有 Codex 会话
synapse sessions codex-list

# 限定工作目录
synapse sessions codex-list -w /path/to/project

# 指定 Codex 数据目录
synapse sessions codex-list --codex-home ~/.codex
```

### 预览和导入

```bash
# 查看会话元信息
synapse sessions codex-inspect <native_id>

# 预览对话内容
synapse sessions codex-preview <native_id>
synapse sessions codex-preview <native_id> -n 50 --offset 100

# 导入为 Synapse 会话
synapse sessions codex-import <native_id>
```

导入时会进行安全检查：过滤内部提示内容、校验文件完整性、跳过不支持的旧版格式。

## 配置

| 变量 | 默认值 | 说明 |
|---|---|---|
| `CHECKPOINT_BACKEND` | `sqlite` | `sqlite`（持久化）或 `memory`（不保存） |
| `CHECKPOINT_PATH` | `.coding-agent/checkpoints.sqlite` | 数据库文件路径 |
| `SESSIONS_PATH` | — | 会话元数据单独存储路径 |

## 对话压缩

当对话上下文接近模型窗口限制时，Synapse 会自动触发压缩（compact）：

- 保留最近的对话 + 之前的摘要
- 默认在 ~85% 窗口时触发，保留 ~10%
- 可通过 `AGENT_ENABLE_COMPACT_TOOL=false` 关闭

## 注意事项

- `memory` 后端不持久化，重启后会话丢失
- 检查点数据库是 SQLite 格式，可以用任何 SQLite 工具查看
- 会话数据存储在项目目录的 `.coding-agent/` 下，纳入 `.gitignore` 建议忽略
