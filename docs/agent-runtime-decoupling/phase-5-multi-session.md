# P5：同项目多会话后台运行

> 状态：Not started  
> 前置条件：P4 detach/reattach 和无订阅者运行门禁通过。  
> 目标：在同一项目中切换会话时，旧会话继续运行。

## 1. 目标

实现当前 workspace 内的多 `SessionRuntime` 管理。TUI 只改变当前 attached thread；运行中的其他会话保持 task、goal、steer 和持久化。

先在同一项目验证并发，避免项目配置隔离问题干扰会话隔离测试。

## 2. RuntimeManager

```python
class RuntimeManager:
    async def open_session(thread_id: str) -> SessionRuntime: ...
    async def submit(thread_id: str, message: UserTurn) -> TurnHandle: ...
    def steer(thread_id: str, text: str) -> bool: ...
    def cancel(thread_id: str) -> bool: ...
    def snapshot(thread_id: str) -> SessionSnapshot: ...
    async def close_session(thread_id: str) -> None: ...
    async def shutdown() -> None: ...
```

内部：

```python
_sessions: dict[str, SessionRuntime]
_global_semaphore: asyncio.Semaphore
```

## 3. Agent graph 与资源共享

第一版采用：

- 每个运行会话独立 Agent graph。
- 每个会话独立 SteerQueue、goal turn 状态和 cancel token。
- 同项目显式共享 model client/cache、checkpointer、MCP tools 和 backend 配置。

原因：当前 steer middleware 和部分状态绑定 graph，直接共用一个 graph 风险高。

必须测量：

- 新增一个 idle graph 的增量内存。
- 新增一个 running graph 的增量内存。
- graph 回收后资源是否释放。

## 4. 并发控制

- 同一 session 使用 `asyncio.Lock` 保证单 graph run。
- 不同 session 可并行，但受全局 semaphore 限制。
- 默认并发上限应保守，并允许配置。
- goal continuation 也占用同一并发配额。
- 排队状态必须可见，不能伪装成 running。

建议状态增加：

```text
queued, starting, running, waiting_approval, cancelling, idle, failed
```

## 5. TUI 语义

切换：

```text
detach(old_thread)
attach(new_thread)
```

不执行：

- `old_task.cancel()`
- 替换全局 `app.agent`
- 清除旧会话 runtime 状态

TUI 应显示：

- 当前会话状态。
- 后台 running 会话数量。
- 会话列表中的状态标记。
- Esc 只取消当前 attached session。
- 输入只 steer 当前 attached session。

## 6. 历史与实时接续

切换到会话时：

1. 获取 snapshot 和 sequence。
2. 重建该 thread transcript projection 尾页。
3. 订阅 sequence 之后事件。
4. 将 buffered terminal/status 合并到当前 chrome。

旧会话迟到事件只进入自己的 broker，不再依赖全局 transcript generation 丢弃。

## 7. 会话生命周期

- `running/waiting_approval/queued` runtime 不可自动回收。
- idle runtime 可在 P8 按 LRU 回收 Agent graph。
- 删除会话前必须检查 runtime；running 会话需先明确取消并等待终态。
- 新建会话在第一条消息前仍可保持不写 metadata 的现有语义。
- 程序退出由 RuntimeManager 统一 shutdown。

## 8. 执行计划

| ID | 工作 | 产物 | 依赖 |
|---|---|---|---|
| P5-01 | 当前项目 RuntimeManager | runtime/manager.py | P4 |
| P5-02 | per-session graph | agent factory/session runtime | P5-01 |
| P5-03 | 项目资源显式共享 | project resource holder（单项目版） | P5-02 |
| P5-04 | 移除 Textual exclusive task 所有权 | ui/turn/tui | P5-01 |
| P5-05 | per-session lock + semaphore | manager/session runtime | P5-01 |
| P5-06 | attach/detach/cancel/steer | UI adapter | P5-04/P5-05 |
| P5-07 | 会话状态 UI | dialog/topbar | P5-06 |
| P5-08 | 并发与串线测试 | tests | P5-06 |
| P5-09 | 多会话门禁 | progress | 全部 |

## 9. 必测竞争场景

- A running 时切 B，B 提交，A/B 都完成。
- A/B token 和 tool event 交错但不串 transcript。
- cancel B 不影响 A。
- A goal 自动续跑时 B 可交互。
- A waiting approval 时 B 正常运行。
- 同一 session 双 submit 只允许一个 active turn。
- session delete 与 active turn 竞争。
- 程序退出与多个 provider/tool wait 竞争。
- model binding 不同的会话使用正确 Agent。

## 10. 验收标准

- 同项目至少两个会话可并发运行。
- UI 切换不取消旧会话。
- 所有事件、usage、goal、summary、cancel 按 thread 隔离。
- Esc 和 steer 只作用于 attached session。
- Textual 不再用 `exclusive=True` 限制全局 Agent turn。
- running runtime 不会被回收或删除。
- 内存增量有测量结果，未出现每会话重复初始化整个进程级资源的问题。

## 11. 风险与回滚

- 风险：per-session Agent graph 内存增长。
  - 缓解：共享昂贵资源、只为实际提交的 session 构建、P8 LRU。
- 风险：共享 checkpointer/MCP 的线程安全问题。
  - 缓解：明确资源契约和交错测试。
- 回滚：保留 RuntimeManager 并发上限为 1 的降级开关，仍使用新架构但关闭并行。
