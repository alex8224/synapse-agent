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

### TUI 恢复会话的分页加载

TUI 启动恢复会话时不会一次性渲染整个 transcript，而是只绘制最后
`history_tail_turns` 轮（默认 20 轮，可在配置中覆盖），避免超长会话
导致启动卡顿。更早的历史通过滚动分页加载：

- 启动后自动滚动到 transcript 底部，显示最近 N 轮。
- 滚动到 transcript 顶部时，异步加载更早的一批历史并插入到当前内容上方，
  滚动位置保持不变，直到加载完整个会话。
- 切换会话（`/session`）或重新加载后，分页状态随 transcript 一起重置。

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

## 全局项目目录（跨项目管理）

会话数据默认按项目（workspace）隔离在各自的 `<workspace>/.synapse/` 下。
Synapse 在用户层维护一份**全局项目目录**（`~/.synapse/catalog.sqlite`），
只读投影每个已注册项目的会话元数据与运行记录，用于跨项目查看与搜索。

### 工作原理

- 每次启动 TUI 时自动注册当前项目（`projects` 表）并投影会话（`project_sessions` 表）；
- 每轮对话结束后，会话摘要增量写入项目库并同步到目录；
- 项目库始终是数据真源，目录只是投影；`projects sync` 可随时全量对账；
- 跨项目引用使用 `(project_id, thread_id)` 复合标识，不修改现有 thread_id。

### 常用命令

```bash
# 列出所有已注册项目（按最近活跃排序）
synapse projects list

# 查看单个项目及其最近会话
synapse projects show <id|名称|路径>

# 列出某个项目的会话（含摘要）
synapse projects sessions <id|名称|路径>

# 跨项目搜索会话标题/摘要
synapse projects search jwt

# 手动对账当前项目的会话到目录
synapse projects sync

# 查看项目运行记录（TUI/CLI 启动历史）
synapse projects runs

# 目录聚合统计
synapse projects stats

# 会话列表跨项目模式
synapse sessions list --all-projects
```

### 配置

| 变量 | 默认值 | 说明 |
|---|---|---|
| `AGENT_PROJECT_CATALOG_ENABLED` | `true` | 启用全局项目目录 |
| `PROJECT_CATALOG_PATH` | `~/.synapse/catalog.sqlite` | 目录数据库路径 |
| `SESSION_SUMMARY_MODE` | `local` | `off` 关闭；`local` 每轮生成确定性本地摘要（不调用模型） |
| `SESSION_SUMMARY_MAX_CHARS` | `600` | 摘要最大字符数（超出裁剪最旧条目） |

### 会话摘要

`local` 模式下，每轮结束后把（任务、工具、进展）合并进会话的
`summary` 字段（`sessions.summary` 列），供全局列表与搜索使用。
摘要只含工具名与回答开头片段，不包含工具输出原文或密钥等敏感内容。

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
