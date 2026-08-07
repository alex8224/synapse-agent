# P8：稳定性、性能与发布收口

> 状态：Not started  
> 前置条件：P7 全局控制面端到端门禁通过。  
> 目标：验证长期运行、资源回收、故障隔离和跨平台行为。

## 1. 目标

在不引入 daemon 的前提下，使单进程多项目、多会话运行达到可长期使用标准，并完成架构文档、配置说明和迁移收口。

## 2. 性能预算制定

P8 不凭空设定阈值；使用 P0 基线和 P5/P6 实测确定预算：

- global landing 冷启动内存。
- 打开只读项目/会话的增量内存。
- 创建 idle SessionRuntime 的增量内存。
- 每个 running session 的增量内存。
- 事件队列最大字节数。
- answer delta 到 TUI 的 P50/P95 延迟。
- cancel 到终态的 P95 时间。
- idle runtime 回收后的可释放资源。

预算必须区分：

- Python allocator 高水位不下降。
- 实际仍被引用的泄漏。
- 模型/MCP/SQLite 必需常驻资源。

## 3. LRU 与资源回收

### 3.1 SessionRuntime

可回收条件：

- status 为 idle/completed/failed。
- 无 active turn。
- 无 pending approval。
- 无 active goal continuation。
- 无 subscriber 或超过 idle 时间。

回收内容：

- Agent graph。
- per-session renderer/buffer。
- session-only model binding resources。

checkpoint 和 transcript 保留，可重新构建。

### 3.2 ProjectRuntime

可回收条件：

- 所有 session 均可回收或已关闭。
- 无 MCP lease。
- 无后台 sync/persistence。

关闭顺序需要固定并测试。

## 4. Shutdown

程序退出流程：

1. RuntimeManager 拒绝新提交。
2. 向 active turn 发 shutdown cancel。
3. 在有界时间内等待 terminal/persistence。
4. 关闭 session subscriptions/brokers。
5. 关闭 Agent graph/checkpointer connections。
6. release MCP leases/pools。
7. 关闭 model HTTP clients。
8. 关闭 SQLite stores/catalog。
9. 停止 AsyncRuntime loop。

超时路径必须记录仍未关闭的 `SessionRef`，但不得无限挂起。

## 5. 故障场景

- provider timeout/rate limit/auth failure。
- tool subprocess timeout和残留子进程。
- MCP server 退出或 reload 竞争。
- SQLite busy/locked/corrupt projection。
- TUI renderer 抛异常或卸载。
- cancel 与 tool completion 同时发生。
- session delete 与 persistence 同时发生。
- project path 在运行期间消失。
- AsyncRuntime shutdown 时仍有 future。

核心 checkpoint 错误与 best-effort projection 错误必须区分，不能统一吞掉。

## 6. 可观测性

所有运行诊断至少携带：

- project_id（可用时）。
- thread_id。
- turn_id。
- runtime status。
- event sequence。

不得记录：

- API key/token。
- `.env` 正文。
- 未脱敏工具输出。
- 用户私有配置正文。

需要提供：

- 当前项目/会话 runtime 数量。
- running/queued/waiting 状态。
- broker backlog。
- 最近 terminal/error。
- MCP/project resource 状态。

## 7. 跨平台

CI 覆盖 Windows/Linux、Python 3.12/3.13。重点检查：

- Windows 路径大小写和盘符 canonicalization。
- 不使用 `fork` 假设。
- PowerShell/bash shell cwd 和 env。
- SQLite WAL 与 busy timeout。
- Textual 跨线程唤醒。
- 文件删除/移动期间的句柄行为。

## 8. 执行计划

| ID | 工作 | 产物 | 依赖 |
|---|---|---|---|
| P8-01 | 长时与压力测试 | tests/bench scripts | P7 |
| P8-02 | 确定性能预算 | baseline/perf report | P8-01 |
| P8-03 | Session/Project LRU | runtime cache | P8-02 |
| P8-04 | running 回收保护 | lifecycle tests | P8-03 |
| P8-05 | 有序 shutdown | runtime cleanup | P8-03 |
| P8-06 | 故障恢复测试 | integration tests | P8-05 |
| P8-07 | 可观测性和泄漏检查 | diagnostics | P8-01 |
| P8-08 | 全量和跨平台门禁 | CI/local verification | P8-06 |
| P8-09 | 文档和余留风险 | docs/changelog | 全部 |

## 9. 验证

```powershell
uv run --no-sync ruff check .
uv run --no-sync pytest -q
uv run --no-sync mkdocs build
uv build
```

如未修改打包配置，`uv build` 可按风险决定；最终发布前必须执行。

还需进行有界 pilot：

- 多项目、多会话交错运行。
- 至少一条长程 goal。
- 多次 attach/detach。
- MCP 启用和禁用组合。
- 退出期间有 active task。

pilot 记录只保留指标和状态，不保存用户正文。

## 10. 验收标准

- 达成并记录性能预算。
- 事件积压和 runtime 数量有界。
- running/waiting session 不被错误回收。
- shutdown 不无限等待且无明显资源泄漏。
- 故障不会串到其他 session/project。
- Windows/Linux 分支均有代码级审查和 CI 覆盖。
- Ruff、全量 pytest、MkDocs build 通过。
- 用户文档、架构文档和迁移说明完整。
- `progress.md` 所有任务完成或明确列为后续非阻塞事项。

## 11. 余留风险格式

最终报告必须列出：

| 风险 | 触发条件 | 用户影响 | 当前缓解 | 后续建议 |
|---|---|---|---|---|

不得用“理论上没问题”代替测量或测试证据。
