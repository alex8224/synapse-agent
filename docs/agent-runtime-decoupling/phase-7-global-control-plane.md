# P7：任意位置启动与全局项目/会话控制面

> 状态：Not started  
> 前置条件：P6 跨项目资源隔离门禁通过。  
> 目标：实现任意位置启动、全局浏览和跨项目会话操作。

## 1. 目标

Synapse 启动目录不再决定唯一工作项目。用户可从全局 landing page 浏览项目、按项目列出会话、打开并操作任意会话；已经运行的其他会话继续执行。

## 2. 启动解析优先级

建议：

1. 显式全局会话引用 `--session <project_id>:<thread_id>`。
2. 显式 `--project <ref>` 或兼容 `--workspace <path>`。
3. cwd 是已注册项目或有 `.synapse/project.json`。
4. 其他位置进入 global landing，不自动注册 cwd。

必须修正当前 `_launch_tui()` 中 `settings.workspace` 与 `project_root=Path.cwd()` 混用的问题。

## 3. Global Bootstrap

新增只读取用户层配置的 bootstrap：

```python
load_global_settings()
```

它可以读取：

- 用户主题和模型目录。
- catalog 路径。
- 全局 UI 设置。
- RuntimeManager 并发和缓存策略。

它不得：

- 创建 `<cwd>/.synapse`。
- 加载 cwd `.env`。
- 把 cwd 注册为项目。
- 打开 cwd sessions/checkpoints。

选择项目后才调用 `load_project_settings(workspace)`。

## 4. 全局 TUI 信息架构

### 4.1 Landing

至少展示：

- 最近项目。
- 最近会话。
- running/queued/waiting 会话。
- 项目 missing/inaccessible/stale 状态。
- 搜索入口。

### 4.2 项目分组会话列表

每项包含：

- project name/path 简写。
- thread/title/model/updated_at。
- runtime status。
- catalog freshness。

选择后解析为唯一 `SessionRef`。

### 4.3 当前会话

Topbar/状态应明确：

- project name。
- workspace。
- session title/id。
- runtime status。
- 后台运行数量。

## 5. 全局操作边界

第一版支持：

- open/attach。
- new session in project。
- submit/steer/cancel。
- rename。
- delete idle session。
- search/list/show。

安全规则：

- running session 删除前必须显式取消并等待。
- catalog stale 时回源确认后再写。
- missing project 只允许查看投影元数据，不允许运行或创建会话。
- 不存在的全局 thread 不自动 `ensure()` 为空会话。
- 歧义名称要求用户选择。

## 6. Catalog 增强

- `sync_project()` 真正对账删除/标记消失的 session 投影。
- 增加 freshness/availability。
- global resolver 返回明确的 none/unique/ambiguous 结果。
- 所有查询有 limit/pagination。
- 启动时不扫描所有项目磁盘；只读 catalog，选择时按需回源。

`project_runs` 继续表示应用启动记录；不要把它误用为 session task 状态。运行状态来自当前进程 RuntimeManager。

## 7. CLI

建议兼容并增强：

```powershell
synapse --project <ref>
synapse --session <project_id>:<thread_id>
synapse projects list
synapse projects sessions <ref>
synapse sessions list --all-projects
```

可增加严格操作命令，但 TUI 和 CLI 必须共用 resolver/RuntimeManager service，不重复实现路径逻辑。

## 8. 执行计划

| ID | 工作 | 产物 | 依赖 |
|---|---|---|---|
| P7-01 | global bootstrap | settings/global loader | P6 |
| P7-02 | 任意 cwd 启动策略 | cli/tui_launch | P7-01 |
| P7-03 | landing/projects view | ui global views | P7-01 |
| P7-04 | grouped session list/search | dialogs/catalog service | P7-03 |
| P7-05 | 回源验证与 freshness | project resolver/catalog | P7-04 |
| P7-06 | 全局 session 操作 | command service/runtime | P7-05 |
| P7-07 | 状态和错误展示 | topbar/dialogs | P7-06 |
| P7-08 | catalog 对账/歧义/清理 | projects/catalog | P7-05 |
| P7-09 | CLI 和文档 | cli/docs | P7-06 |
| P7-10 | 端到端门禁 | tests/pilot | 全部 |

## 9. 端到端场景

- 从临时目录启动，不创建 `.synapse`，能看到已注册项目。
- 从用户主目录启动，不把 home 注册为项目。
- 打开项目 A 会话并运行，切换项目 B 会话，A 继续。
- A/B 同 thread_id 仍正确路由。
- 项目目录 missing 时显示状态，不在当前目录误运行。
- 两个同名项目要求选择。
- catalog session 已删除时，回源后刷新投影并拒绝操作。
- running session rename 正确更新本地真源和 catalog 投影。
- 程序退出关闭所有项目和会话 runtime。

## 10. 验收标准

- 任意位置启动可访问所有注册项目。
- 项目和会话按唯一复合身份操作。
- 选择会话后 Agent 的 filesystem/shell/config 属于其项目。
- 切换项目/会话不取消其他运行任务。
- cwd 不被错误注册或污染。
- catalog stale/missing/ambiguous 都有明确处理。
- CLI、TUI 和文档行为一致。

## 11. 风险与回滚

- 风险：默认启动行为变化影响现有用户。
  - 缓解：已识别项目 cwd 继续直接进入项目；其他位置才显示 landing。
- 风险：全局操作误删错误项目会话。
  - 缓解：SessionRef、回源验证、运行态删除门禁和确认信息。
- 回滚：保留 `--workspace` 明确进入单项目模式；global landing 可临时通过内部开关关闭。
