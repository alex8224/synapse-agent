# P1：Streaming Core 与事件契约

> 状态：Completed（2026-08-07）  
> 前置条件：P0 门禁通过。  
> 目标：流解析和最终结果不再依赖 UI sink 的可变状态。

## 1. 目标

建立 UI-independent 的流事件契约、运行时 accumulator 和兼容 adapter，将 LangGraph 原始事件解释为可供任意前端消费的语义事件。

P1 结束时，现有 CLI/TUI 行为保持不变，但核心解析代码可以在不导入 `synapse.ui` 的环境中测试。

## 2. 核心问题

当前 `stream_agent()` 同时承担：

- 选择 async/sync stream。
- 解析 LangGraph messages/updates。
- answer/reasoning/tool 去重与匹配。
- usage 和 token rate 聚合。
- cancel、repair、HITL 检测。
- 调用 UI sink。
- 从 `sink.answer_buf`、`sink.reasoning_buf` 和 `sink.streamed_answer` 构造结果。

最后一项意味着消费者是运行结果真源，无法支持无订阅者运行。

## 3. 目标模块

建议新增：

```text
src/synapse/runtime/streaming/
  __init__.py
  events.py
  accumulator.py
  protocol.py
  normalize.py
  runner.py
  adapters.py
```

迁移期保留：

```text
src/synapse/ui/stream.py          Rich renderer + 兼容 re-export
src/synapse/ui/stream_events.py   兼容 re-export
src/synapse/ui/stream_runtime.py  兼容 re-export
src/synapse/ui/sink.py            兼容协议入口
```

## 4. 事件契约

### 4.1 Envelope

```python
@dataclass(frozen=True, slots=True)
class TurnEvent:
    version: int
    thread_id: str
    turn_id: str
    sequence: int
    kind: TurnEventKind
    payload: TurnEventPayload
```

约束：

- `sequence` 从 1 开始，在单个 turn 内严格递增。
- `version` 初始为 1；破坏性 payload 变化必须提升版本。
- payload 不含 Textual、Rich renderable、SQLite connection 或 Agent 实例。
- completed/failed/cancelled 是互斥终态，最多出现一次。

### 4.2 Payload

建议使用 typed dataclass，而不是无约束 `dict[str, Any]`：

```python
ActivityChanged(phase, detail, reset_timer)
TextDelta(text, message_id)
TextCompleted(text, message_id)
ToolCallSnapshot(call_id, name, args, intent)
ToolItemSnapshot(id, call_id, name, category, label, path, parent_id, sub)
ToolFinished(id, status, preview, error, workspace_changed)
UsageSnapshot(...)
TurnTerminal(status, result_summary, error)
```

原始工具 args 必须有界。超大或不可序列化字段使用安全摘要；不要把工具输出正文复制进事件。

## 5. `TurnAccumulator`

Runtime-owned accumulator 保存：

- 已见 message/tool/usage ID。
- answer delta/open/complete 文本。
- reasoning delta/open/complete 文本。
- pending tool items 及 parent/subagent 归属。
- usage 总计和最后一次调用数据。
- compact 次数。
- streamed flags。
- 当前状态和终态。

`StreamResult` 只能从 accumulator 和最终 graph state 构造；不能访问 renderer 或 sink 缓冲区。

为防止无界内存：

- answer/reasoning 的最终正文按现有行为保留，但实时 preview 不重复复制全量字符串。
- info/activity 事件不进入长期 accumulator。
- tool preview 继续使用现有限额。
- dedupe set 在 turn 完成后整体释放。

## 6. Adapter 策略

### 6.1 新接口

```python
class AgentEventSink(Protocol):
    def emit(self, event: TurnEvent) -> None: ...
```

实现：

- `NullEventSink`
- `CollectingEventSink`
- `CompositeEventSink`
- `CallbackEventSink`

### 6.2 Legacy 兼容

`LegacyStreamSinkAdapter` 将标准事件映射回现有 `StreamSink` 方法：

```text
answer_delta       -> write_answer_token
answer_completed   -> write_answer_complete
reasoning_delta    -> write_reasoning
tool_started       -> tool_item_started
usage_updated      -> note_usage
info               -> info
```

旧 `stream_agent(..., sink=TextualStreamSink(...))` 在迁移期内部自动包装，不改变调用方。

## 7. Tool 模型迁移

`ToolItem` 当前位于 `synapse.ui.timeline`，但本身是纯数据模型。P1 应将运行领域模型移动到 runtime/content 中立位置，例如：

```text
src/synapse/runtime/streaming/tool_events.py
```

UI 的分类、图标、颜色和 group summary 仍留在 `ui.timeline`。

迁移要求：

- 保留 `synapse.ui.timeline.ToolItem` re-export。
- 生产代码先使用新路径。
- 搜索扩展调用方后再决定何时删除兼容导出。

## 8. Runner 抽取

`runner.py` 负责：

1. 接收原始 normalized stream item。
2. 更新 accumulator。
3. 产生 `TurnEvent`。
4. 向 sink emit。
5. 构造 `StreamResult`。

Rich console、Textual host、DOM、Git chrome 和 modal 状态不得进入该模块。

原始 stream 获取可暂时继续使用当前同步桥；真正的 `AgentTurnRuntime.arun()` 在 P2 完成。

## 9. Retry 并发安全

当前 module-level `_retry_notifier` 会被并发 turn 覆盖。P1 改为 `ContextVar` 或等价的显式作用域：

```python
token = retry_notifier_var.set(callback)
try:
    ...
finally:
    retry_notifier_var.reset(token)
```

测试必须交错运行两个 notifier，确保 retry 信息回到正确的 collecting sink。

## 10. 执行计划

| ID | 工作 | 主要文件 | 依赖 |
|---|---|---|---|
| P1-01 | 定义事件和 payload | runtime/streaming/events.py | P0 |
| P1-02 | 迁移 ToolItem 领域模型 | runtime/streaming + ui re-export | P1-01 |
| P1-03 | 实现 accumulator | accumulator.py | P1-01 |
| P1-04 | 实现新 sink | protocol.py | P1-01 |
| P1-05 | 实现 legacy adapter | adapters.py | P1-02/P1-04 |
| P1-06 | 抽取 normalizer/runner | normalize.py/runner.py | P1-03/P1-04 |
| P1-07 | retry notifier 作用域化 | runtime/middleware.py | P1-04 |
| P1-08 | 增加旧路径 re-export 并迁移内部调用 | ui/stream*.py | P1-06 |
| P1-09 | 运行契约和兼容门禁 | tests | 全部 |

## 11. 测试

新增建议：

```text
tests/test_runtime_stream_events.py
tests/test_runtime_stream_accumulator.py
tests/test_runtime_stream_adapters.py
tests/test_retry_notifier_scope.py
```

重点断言：

- 无 sink 时结果完整。
- collecting sink 丢弃任意 preview 事件不影响最终结果。
- sequence 严格递增。
- 终态唯一。
- raw LangChain 对象不会泄漏进 event payload。
- legacy adapter 产生与 P0 trace 相同的行为。

## 12. 验收标准

- `synapse.runtime.streaming` 不导入 `synapse.ui` 或 Textual。
- `StreamResult` 不读取外部 sink 属性。
- 现有 CLI/TUI 流测试全部通过。
- 旧公共导入路径仍可用。
- 两个并发 retry notifier 不互相覆盖。
- P0 的语义 trace 无非预期变化。

## 13. 风险与回滚

- 风险：去重逻辑在 runtime 和 legacy sink 中重复，导致少消息或重复消息。
  - 缓解：P0 trace 驱动，先 adapter 后删旧状态。
- 风险：事件 payload 复制超大 args。
  - 缓解：契约层统一有界 snapshot。
- 回滚：保留旧 `stream_agent` 入口；runner 切换应是单独提交，可恢复旧实现而不影响数据。
