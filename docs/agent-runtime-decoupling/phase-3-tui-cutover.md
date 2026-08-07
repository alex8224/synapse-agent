# P3：TUI 事件适配与切换

> 状态：Completed（2026-08-07）  
> 前置条件：P2 headless AgentTurnRuntime 门禁通过。  
> 里程碑：本阶段完成后，Agent loop 与 TUI 生命周期正式解耦。

## 1. 目标

让现有 Textual TUI 通过标准 `TurnEvent` 实时渲染，不再作为 Agent stream sink 或 turn 状态真源。

为降低风险，P3 仍可由 Textual worker 发起并等待一个 turn，仍保持单活动 turn；后台会话和多会话从 P4/P5 开始。

## 2. 目标链路

```text
TurnController
  -> AgentTurnRuntime
      -> TurnEvent
          -> thread-safe bridge
              -> TextualTurnEventRenderer
                  -> TranscriptController / chrome
```

运行层不知道 renderer 是否存在。renderer 被销毁、generation 改变或暂时不消费时，runtime 仍完成。

## 3. Renderer Host 协议

新增窄协议，例如：

```python
class TextualTurnEventHost(Protocol):
    def call_from_thread(...): ...
    def set_activity(...): ...
    def set_stream(...): ...
    def commit_thought(...): ...
    def commit_answer(...): ...
    def write_tool_item(...): ...
    def update_tool_item(...): ...
    def apply_turn_usage(...): ...
```

禁止 renderer 通过 `Any app` 任意访问 `_busy`、settings、thread 或 runtime task。

## 4. 事件桥

Agent loop 与 Textual loop 位于同一进程不同线程，不能直接共享 `asyncio.Queue`。

P3 使用有界线程安全结构：

```text
Agent thread/loop
  -> deque + Lock
  -> 空到非空时仅唤醒 UI 一次
  -> Textual 主线程批量 drain
```

规则：

- answer/reasoning delta 可在 16-50ms 窗口合并。
- activity 更新可覆盖旧值。
- tool complete、error、cancel、terminal 不可丢。
- 单次 drain 有事件数/时间预算，避免饿死 UI loop。
- renderer 只在 Textual 主线程修改 DOM。

P4 会将该桥演进为按会话的 EventBroker；P3 只实现单 turn adapter。

## 5. Generation 语义

当前 `TextualStreamSink` 在构造时捕获 transcript generation。P3 将过滤放在订阅/renderer 边界：

- runtime 始终发出完整事件。
- renderer 记录自己绑定的 `thread_id/turn_id/generation`。
- generation 不匹配时只停止渲染，不影响 runtime 和 result。
- terminal/result 仍由 TurnController 收到，用于现阶段收尾。

## 6. 现有 TextualStreamSink 迁移

采用两步法：

1. `TextualTurnEventRenderer` 先复用 `TextualStreamSink` 的展示状态机，将事件映射为现有方法。
2. 行为稳定后，再把纯展示逻辑直接收敛进 renderer。

P3 不要求一次性删除 `TextualStreamSink`，但生产 Agent runner 不再直接持有它。

UI-only 策略继续保留：

- live preview 限频和 tail preview。
- Thought/Answer block 挂载。
- ToolGroup 折叠和 summary。
- DAG task group 抑制。
- workspace 变更后的 Git chrome 刷新。

## 7. TurnController 改造

`TurnController` 只负责：

- 解析用户输入和附件。
- 处理 slash command。
- 创建冻结 `TurnContext`。
- 请求 AgentTurnRuntime 执行。
- 将事件 bridge 绑定 renderer。
- turn 完成后调用当前兼容收尾逻辑。

它不再：

- 解析 LangGraph stream。
- 直接构造 `TextualStreamSink` 给 runner。
- 从 transcript widget 推导 runtime result。

## 8. 收尾兼容

P3 暂时保持以下行为在 TUI 层：

- `_busy` 单实例门禁。
- goal settle/continue。
- session recap。
- transcript projection。
- summary/catalog upsert。
- follow-up steer。

但这些收尾必须使用本次 `TurnContext` 和 `TurnResult`，禁止读取可能已变化的 `app.thread_id`。P4 再将其移入 SessionRuntime。

## 9. 执行计划

| ID | 工作 | 产物 | 依赖 |
|---|---|---|---|
| P3-01 | 定义 renderer host 协议 | ui/turn/event_renderer.py | P2 |
| P3-02 | 映射标准事件 | event renderer/adapter | P3-01 |
| P3-03 | 实现批量 bridge 和刷新预算 | ui/turn/event_bridge.py | P3-01 |
| P3-04 | 迁移 generation 过滤 | renderer/subscription | P3-02 |
| P3-05 | TurnController 调度 runtime | ui/turn/controller.py | P3-03 |
| P3-06 | 冻结收尾上下文 | persistence/goal adapters | P3-05 |
| P3-07 | 切换生产路径并保留兼容入口 | tui/stream compatibility | P3-06 |
| P3-08 | Textual pilot 和性能回归 | tests | P3-07 |
| P3-09 | 解耦里程碑验收 | progress/architecture check | 全部 |

## 10. 关键测试

- 同一 P0 trace 通过 legacy renderer 和新 event renderer，block 顺序一致。
- 高频 token 不产生每 token 一次 `call_from_thread()`。
- generation 改变后旧 renderer 不更新 DOM，但 runtime result 正确。
- renderer 抛错/卸载不取消 turn。
- tool group 在 cancel 和异常时正确闭合。
- usage 不重复累计。
- TUI 关闭时 bridge 停止唤醒，不导致后台 loop 异常。

最窄验证：

```powershell
uv run --no-sync pytest tests/test_textual_stream_sink.py -q
uv run --no-sync pytest tests/test_textual_stream_sink.py tests/test_turn_controller.py -q
uv run --no-sync pytest tests/test_stream_tool_items.py tests/test_transcript_history_controller.py -q
```

## 11. 里程碑验收

必须证明：

1. `AgentTurnRuntime` 不持有 Textual host。
2. 运行中销毁或替换 renderer，turn 仍完成。
3. 无 renderer 时 result、checkpoint 和终态正确。
4. TUI 实时 token、reasoning、tool 和 usage 行为无非预期退化。
5. 生产调用路径不再把 `TextualStreamSink` 直接传给 Agent runner。
6. runtime 目录通过 UI 导入护栏。

P3 验收通过后才允许开始 P4。

## 12. 风险与回滚

- 风险：事件桥积压造成内存增长。
  - 缓解：delta 合并、有界 drain、不可丢事件白名单。
- 风险：复用旧 sink 形成双状态机。
  - 缓解：明确 runtime accumulator 是真源，旧 sink 只做展示。
- 回滚：保留旧生产路径到 P3 门禁完成；切换点保持单一且可逆。
