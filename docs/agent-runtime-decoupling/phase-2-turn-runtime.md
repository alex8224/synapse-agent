# P2：AgentTurnRuntime

> 状态：Completed（2026-08-07）  
> 前置条件：P1 事件契约和 runner 门禁通过。  
> 目标：一次 turn 可完全脱离 Textual 独立执行。

## 1. 目标

建立 `AgentTurnRuntime`，使 fake/真实 Agent 能在无 `CodingAgentApp`、无 widget、无 `call_from_thread()` 的情况下完成一次普通提交或 HITL resume。

P2 只处理单个 turn，不负责多个 turn 的 goal 自动续跑和 session 生命周期；这些属于 P4。

## 2. API 草案

```python
@dataclass(frozen=True)
class TurnContext:
    thread_id: str
    turn_id: str
    agent: Any
    settings: Settings
    request: TurnRequest

class AgentTurnRuntime:
    async def arun(
        self,
        context: TurnContext,
        *,
        sink: AgentEventSink | None = None,
        cancel_token: CancelToken | None = None,
    ) -> TurnResult: ...

    def run(...):
        """仅供非 Agent runtime loop 的同步兼容调用方使用。"""
```

`run()` 必须检测是否在绑定 loop 上调用，禁止在同一个 loop 中阻塞等待自己。

## 3. 状态机

```text
created
  -> running
      -> cancelling -> cancelled
      -> waiting_approval
      -> completed
      -> failed
```

规则：

- 每个实例/handle 只运行一次。
- cancel 是幂等的。
- `completed`、`cancelled`、`waiting_approval`、`failed` 互斥。
- 所有路径都释放 notifier、临时 accumulator 和 stream producer。
- terminal event 在资源收尾后发出，且只发一次。

## 4. 调度模型

默认路径：

- `AgentTurnRuntime.arun()` 在现有 `AsyncRuntime.loop` 上执行。
- AsyncSqliteSaver 与 `agent.astream()` 使用同一绑定 loop。
- TUI/CLI 从其他线程通过 `run_coroutine_threadsafe()` 提交。

兼容路径：

- sync-only checkpointer/agent stream 放到有界 worker thread。
- 事件仍通过线程安全 sink 发布。
- 不允许 sync fallback 阻塞 Agent loop。

P2 不增加第二个 Agent loop。

## 5. 请求构造迁移

当前 `ui/turn/request.py` 的 `TurnRequest` 和 `build_turn_request()` 属于运行领域，应迁移到：

```text
src/synapse/runtime/agent_loop/request.py
```

保留旧路径 re-export。

请求中必须冻结：

- `thread_id`
- payload/attachments
- monitor ID
- max concurrency
- HITL resume payload
- 运行配置扩展

运行期间不能重新读取 `app.thread_id` 或当前 model picker 状态。

## 6. 取消与修复

`CancelToken` 封装当前 `threading.Event` 语义，并为后续 async task cancel 留出接口：

```python
class CancelToken:
    def cancel(self, reason: str = "user") -> bool: ...
    @property
    def cancelled(self) -> bool: ...
```

取消流程：

1. 设置 token。
2. 停止/取消 stream producer。
3. accumulator 将运行中的工具标记为 cancelled。
4. 调用 `repair_thread_after_cancel()`。
5. 发出唯一 `turn_cancelled`。
6. 返回 `TurnResult(cancelled=True)`。

取消 repair 失败应记录诊断，但不能把 cancelled 错报成 completed。

## 7. 错误与 HITL

- provider/tool/runtime 异常转为 `turn_failed`，保留异常类型和安全消息。
- 默认不吞核心执行错误；由上层决定如何显示。
- HITL pending 转为 `waiting_approval` 终态，不是 failed。
- retry 信息通过当前 turn 的事件 sink 发布。
- error payload 不含 API key、环境变量或完整私有请求。

## 8. `TurnHandle`

```python
class TurnHandle:
    turn_id: str
    future: concurrent.futures.Future[TurnResult]
    cancel_token: CancelToken

    def cancel(self, reason="user") -> bool: ...
    def done(self) -> bool: ...
    def result(self, timeout=None) -> TurnResult: ...
```

P2 可先用于 headless 和 TUI worker；P4 再由 SessionRuntime 持有它。

## 9. 执行计划

| ID | 工作 | 产物 | 依赖 |
|---|---|---|---|
| P2-01 | 定义 context/status/result/handle | runtime/agent_loop/model.py | P1 |
| P2-02 | 实现 `arun()` | runtime/agent_loop/turn.py | P2-01 |
| P2-03 | 实现同步兼容和 sync fallback | turn.py | P2-02 |
| P2-04 | 迁移 TurnRequest | runtime/agent_loop/request.py | P2-01 |
| P2-05 | 纳入 cancel/repair/error/HITL | turn.py | P2-02 |
| P2-06 | 终态唯一与清理 | turn.py/tests | P2-05 |
| P2-07 | 添加 headless 测试 | tests/test_agent_turn_runtime.py | P2-06 |
| P2-08 | 执行 API 和资源门禁 | progress 验证记录 | 全部 |

## 10. 测试

必须覆盖：

- 无 sink 正常完成。
- 无订阅者时 answer/result 完整。
- sink 回调抛异常时 runtime 的策略明确且有测试。
- cancel during model、cancel during tool。
- HITL waiting。
- transient retry。
- provider failure。
- sync saver fallback。
- 从 Agent loop 内错误调用同步 `run()` 会快速失败而非死锁。
- 终态事件只出现一次。

## 11. 验收标准

- `AgentTurnRuntime` 及其测试不创建 Textual App。
- `arun()` 是默认主路径。
- 一次 turn 的所有输入在开始时冻结。
- 结果和终态不依赖 sink 是否存在。
- cancel、HITL、error、normal completion 都有唯一终态。
- runtime 包不导入 UI。
- P0/P1 全部回归通过。

## 12. 风险与回滚

- 风险：在绑定 loop 上调用同步 wrapper 导致死锁。
  - 缓解：显式 loop 检测和测试。
- 风险：sync fallback 事件来自不同线程。
  - 缓解：sink 契约声明线程安全；UI adapter 不直接在该线程改 DOM。
- 回滚：保留旧 `stream_agent` 同步路径；P2 API 在 P3 切换前不替换生产 TUI。
