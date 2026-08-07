# P0：基线与护栏

> 状态：Completed（2026-08-07）  
> 前置条件：总体方案与 ADR 已确认。  
> 后续阶段：P1 Streaming core 与事件契约。

## 1. 目标

在不改变生产行为的前提下，固化当前 Agent stream 和 TUI 的可观察语义，建立重构期间能及时发现串线、漏事件和性能退化的测试护栏。

P0 不做实现迁移，只增加测试、fixtures、依赖约束和基线报告。

## 2. 非目标

- 不新增 runtime API。
- 不移动 `stream_agent`。
- 不修改当前 TUI 调度方式。
- 不开放并行 turn。
- 不改变用户配置或数据库。

## 3. 当前关键路径

```text
TurnController.submit
  -> CodingAgentApp.run_turn (@work exclusive)
  -> TurnController.run_turn
  -> stream_agent
  -> _iter_stream_events
  -> TextualStreamSink
  -> TranscriptController
```

必须覆盖的耦合点：

| 类别 | 当前位置 | P0 要固化的行为 |
|---|---|---|
| 执行 | `ui/turn/controller.py` | agent/thread 冻结、busy、cancel、turn_done |
| 原始流 | `ui/stream_runtime.py` | async/sync saver、heartbeat、cancel |
| 语义解析 | `ui/stream.py` | 去重、工具匹配、usage、compact、HITL |
| TUI 渲染 | `ui/textual_stream_sink.py` | block 顺序、限频、工具组收尾 |
| 收尾 | `ui/turn/persistence.py` | transcript、summary、catalog |
| 全局槽 | `runtime/middleware.py` | retry notifier 当前单槽行为 |

## 4. 行为矩阵

P0-01 产出的行为矩阵至少包含：

| 场景 | 原始输入 | 事件/显示顺序 | 最终结果 | 终态 |
|---|---|---|---|---|
| 纯 answer token | messages delta + final update | activity -> deltas -> answer complete | final_text | completed |
| reasoning + answer | reasoning delta + text | thought -> answer | reasoning_text/final_text | completed |
| 多轮 tool loop | AI tool calls -> tool result -> AI | tool group独立闭合 | tool_calls | completed |
| 并行工具 | 多个 calls/results | item ID 和结果匹配稳定 | count/preview | completed |
| nested subagent | namespace 交错 | parent 归属正确 | no parent pollution | completed |
| steer | model-only HumanMessage | 不进入可见 answer | empty/previous answer | completed |
| compact | summary wrapper | 只发 compact info | compact_events | completed |
| HITL | pending interrupt | approval 提示 | interrupted=True | waiting |
| cancel | cancel during model/tool | running item 收尾 | cancelled=True | cancelled |
| retry | transient model error | retry info 属于当前 turn | result/error | completed/failed |

## 5. Fixture 设计

建议新增：

```text
tests/fixtures/agent_stream/
  answer.json
  reasoning_answer.json
  tool_round.json
  parallel_tools.json
  nested_subagents.json
  compact.json
```

如果 LangChain message 无法稳定 JSON 化，使用小型 Python fake-agent builder；fixture 只保存纯数据事件，不保存 provider 私有对象。

每个 fixture 必须有：

- 输入 raw stream 顺序。
- 预期语义 trace。
- 预期 `StreamResult` 摘要。
- 是否包含 UI-only 断言。

## 6. 依赖护栏

新增测试或静态检查，确保未来目标目录不依赖 UI：

```text
synapse.runtime.agent_loop -> 禁止 import textual
synapse.runtime.agent_loop -> 禁止 import synapse.ui
synapse.runtime.streaming  -> 禁止 import textual
```

P0 时目标目录可能尚不存在，检查应允许目录缺失；P1 创建目录后自动生效。

## 7. 性能基线

记录而非预设不现实的固定阈值：

- 冷启动到 TUI mounted。
- Agent ready 时间。
- 第一 answer token 到 UI 的延迟。
- 10k/50k 字符输出的 UI 刷新次数和耗时。
- 单 turn 完成后的常驻内存。
- 会话切换前后内存高水位。
- cancel 到 runner 返回的时间。

基线报告建议保存为：

```text
docs/agent-runtime-decoupling/baseline-report.md
```

报告不得包含密钥、用户消息正文或私有路径明细。

## 8. 执行计划

| ID | 工作 | 产物 | 依赖 |
|---|---|---|---|
| P0-01 | 建立行为矩阵 | 本文或 baseline report | 无 |
| P0-02 | 增加 raw stream fixtures/fake agents | tests fixtures/helpers | P0-01 |
| P0-03 | 增加有序 trace 测试 | stream contract tests | P0-02 |
| P0-04 | 补 cancel/retry/HITL 等边界 | targeted tests | P0-02 |
| P0-05 | 增加导入护栏 | architecture dependency test | 无 |
| P0-06 | 采集性能和内存基线 | baseline-report.md | P0-03 |
| P0-07 | 执行门禁 | progress 验证记录 | 全部 |

## 9. 验证

最窄测试：

```powershell
uv run --no-sync pytest tests/test_stream_cancel.py -q
uv run --no-sync pytest tests/test_stream_tool_items.py -q
uv run --no-sync pytest tests/test_textual_stream_sink.py -q
uv run --no-sync pytest tests/test_turn_controller.py -q
```

阶段门禁：

```powershell
uv run --no-sync ruff check .
uv run --no-sync pytest -q
```

## 10. 验收标准

- 行为矩阵覆盖所有列出的场景。
- fake-agent trace 可重复，不依赖网络。
- cancel、HITL、retry、compact 和 nested subagent 都有回归测试。
- 导入护栏能在故意加入 UI import 时失败。
- 基线报告记录测试环境、方法和结果。
- 生产代码行为未改变。

## 11. 风险与回滚

- 风险：测试过度绑定 DOM 实现，使后续合法重构困难。
  - 缓解：语义 trace 与 Textual 展示测试分层。
- 风险：性能测量受机器波动影响。
  - 缓解：记录环境、重复次数和范围，不在 P0 设置过窄硬阈值。
- 回滚：P0 只新增测试和文档，可独立回退，不涉及数据迁移。
