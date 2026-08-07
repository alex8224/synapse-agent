# P4：SessionRuntime 与事件 Broker

> 状态：Completed（2026-08-07）  
> 前置条件：P3 解耦里程碑通过。  
> 目标：会话执行生命周期不再依赖当前 TUI 页面或订阅者。

## 1. 目标

引入单会话 `SessionRuntime` 和有界 `SessionEventBroker`，将 task、cancel、steer、usage、goal 和持久化从 `CodingAgentApp` 移出。

P4 重点证明 detach/reattach；暂不开放多个会话同时运行，避免同时调试生命周期和并发。

## 2. Session 状态机

```text
cold -> idle -> running -> idle
                  |       -> failed
                  |       -> waiting_approval
                  -> cancelling -> idle/cancelled
```

状态快照至少包含：

```python
@dataclass(frozen=True)
class SessionSnapshot:
    thread_id: str
    status: SessionStatus
    active_turn_id: str | None
    latest_sequence: int
    usage: SessionUsage
    goal: GoalSnapshot | None
    last_error: str | None
```

## 3. 命令接口

```python
class SessionRuntime:
    async def submit(message: UserTurn) -> TurnHandle: ...
    def steer(text: str) -> bool: ...
    def cancel(reason: str = "user") -> bool: ...
    def snapshot() -> SessionSnapshot: ...
    def subscribe(after_sequence: int = 0) -> Subscription: ...
    async def close() -> None: ...
```

同一 SessionRuntime 的 `submit()` 在 running 时不启动第二个 graph run；普通输入按现有语义进入 steer 或被明确拒绝。

## 4. SessionEventBroker

Broker 为跨 turn 的事件分配 session-local sequence：

```python
@dataclass(frozen=True)
class SessionEventEnvelope:
    thread_id: str
    sequence: int
    turn_id: str
    event: TurnEvent
```

### 4.1 Snapshot + Subscribe

为避免恢复历史和订阅实时流之间丢事件：

1. UI 获取 `snapshot.latest_sequence = N`。
2. 加载 transcript projection。
3. 调用 `subscribe(after_sequence=N)`。
4. Broker 返回 N 之后的缓冲事件并继续实时推送。

订阅必须支持幂等关闭。

### 4.2 有界策略

- 每会话 ring buffer 有事件数和字节预算。
- 高频 delta 可合并或淘汰旧 preview。
- terminal、tool completion、usage final 和 error 不可静默丢失。
- 持久化 transcript 是长期恢复真源；broker 只负责实时接续。
- 慢订阅者不能阻塞 Agent loop。

## 5. 状态迁移

从 `CodingAgentApp` 移出：

- `_busy`
- `_cancel_event`
- `_active_turn_agent`
- `_active_turn_thread_id`
- `_active_steer_queue`
- 当前 turn usage
- goal settle/continue
- turn task/handle

TUI 保留当前会话 snapshot 的展示副本，但不能成为真源。

## 6. 持久化迁移

当前 `TurnPersistenceController` 从 transcript widget 提取 user/tool/answer。P4 改为 runtime 事件 accumulator 驱动：

- user 输入来自冻结 `TurnContext`。
- thought/answer/tool 来自 `TurnResult` 或事件 accumulator。
- usage 来自 SessionRuntime。
- thread/workspace 来自 SessionRuntime 创建时的固定上下文。

持久化顺序建议：

1. graph checkpoint（LangGraph 自身）。
2. transcript projection。
3. session metadata/summary。
4. global catalog projection（best effort）。
5. 发布最终 session snapshot。

checkpoint 是核心真源；projection 失败不应把 completed 改为 failed，但必须记录诊断。

## 7. Goal 与 Follow-up

Goal 自动续跑必须在无 UI 时仍工作：

- `on_turn_end(thread_id)` 在 SessionRuntime 中执行。
- 用户 steer 优先于 goal continuation。
- cancel 暂停当前 goal。
- continuation 创建下一个 turn，而不是依赖 Textual `call_after_refresh()`。
- 增加循环保护和已有预算规则。

Session recap 作为展示增强可以暂留 UI；与执行相关的 summary/catalog 不可留在 UI。

## 8. 执行计划

| ID | 工作 | 产物 | 依赖 |
|---|---|---|---|
| P4-01 | SessionRuntime 状态机/API | runtime/sessions/runtime.py | P3 |
| P4-02 | SessionEventBroker | runtime/sessions/events.py | P4-01 |
| P4-03 | snapshot+subscribe | broker/subscription | P4-02 |
| P4-04 | 迁移 task/cancel/steer | session runtime | P4-01 |
| P4-05 | 迁移 usage | session snapshot | P4-04 |
| P4-06 | 重写上下文驱动持久化 | runtime persistence | P4-05 |
| P4-07 | 迁移 goal/follow-up | session orchestration | P4-04 |
| P4-08 | TUI attach/detach adapter | ui session binding | P4-03/P4-07 |
| P4-09 | 背压和终态保留 | broker tests | P4-02 |
| P4-10 | detach 门禁 | tests/progress | 全部 |

## 9. 测试

必须覆盖：

- 无任何 subscriber 时 turn 完成并持久化。
- turn 中途 detach 后继续完成。
- reattach 不丢 completed/tool/error 事件。
- snapshot 与事件游标无重复/缺口。
- cancel 不依赖当前页面。
- goal 在无 UI 时继续，用户 steer 优先。
- projection 失败时 checkpoint 和 session 状态明确。
- 慢订阅者不阻塞模型流。
- close 时取消或等待 active turn 的策略一致。

## 10. 验收标准

- TUI 不再拥有 busy/task/cancel/goal 真状态。
- SessionRuntime 在无 subscriber 时可完成普通 turn 和 goal continuation。
- transcript/summary/catalog 不再读取 widget 状态。
- attach/detach 只影响渲染，不影响执行。
- broker 有界且终态不可丢。
- 仍保持单活动会话，避免提前引入 P5 并发。

## 11. 风险与回滚

- 风险：projection 与实时事件重复挂载。
  - 缓解：sequence cursor 和去重测试。
- 风险：goal continuation 无 UI 后形成失控循环。
  - 缓解：复用目标预算、明确终态和循环门禁。
- 回滚：P4 切换前保留 P3 的 TUI 收尾 adapter；数据库格式不变。
