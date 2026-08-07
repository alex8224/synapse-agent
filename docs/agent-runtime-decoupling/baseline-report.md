# P0 Agent Stream 基线报告

> 状态：P0 基线已固化  
> 日期：2026-08-07  
> 原则：只记录可重复测试和脱敏指标，不记录用户消息、密钥或私有配置正文。

## 1. 当前行为基线

当前生产链路：

```text
TurnController.submit
  -> CodingAgentApp.run_turn (@work thread=True, exclusive=True)
  -> TurnController.run_turn
  -> stream_agent
  -> _iter_stream_events
  -> TextualStreamSink
  -> TranscriptController
```

### 行为矩阵

| 场景 | 当前语义 | 主要回归测试 |
|---|---|---|
| answer token + final update | delta 实时显示，完整消息只提交一次 | `test_runtime_streaming.py`, `test_stream_ui.py` |
| reasoning + answer | reasoning 先闭合，再提交 answer | `test_textual_stream_sink.py`, `test_stream_ui.py` |
| tool round | tool call/result 匹配并独立闭合 group | `test_stream_tool_items.py` |
| parallel tools | 以 call id 优先匹配，不随结果顺序串项 | `test_stream_tool_items.py` |
| nested subagents | nested item 归属对应 parent task | `test_stream_tool_items.py` |
| steer | model-only steer 不进入可见 answer | `test_stream_tool_items.py` |
| context compact | summary wrapper 隐藏，仅记录 compact | stream 相关测试 |
| HITL | 保留结果并标记 `interrupted=True`，提示 approve/reject | `test_stream_semantic_fixtures.py` |
| cancel | producer 被取消，checkpoint best-effort repair | `test_stream_cancel.py` |
| sync saver fallback | async 不兼容时回退 sync stream | `test_stream_cancel.py` |
| session switch late event | generation 变化后旧 renderer 丢弃回调 | `test_textual_stream_sink.py` |

## 2. P1 新基线

P1 引入 runtime-owned `TurnAccumulator` 后，增加以下契约：

- `StreamResult` 不再读取 Rich/Textual renderer 的 answer/reasoning buffer。
- runtime 事件携带 `thread_id`、`turn_id` 和严格递增 `sequence`。
- observer 失败不影响 Agent turn。
- `synapse.runtime.streaming` 不依赖 `synapse.ui` 或 Textual。
- retry notifier 使用 `ContextVar` 隔离并发 turn。
- 旧 `StreamSink` 和 `synapse.ui.stream` 调用方式保持兼容。

## 3. 性能测量方法

测试环境：Windows 11 `10.0.26200`、Python 3.12.9、AMD64 Family 23 Model 96。所有数据均为本机观测值，不作为跨平台硬阈值。

| 指标 | 方法 | 结果 |
|---|---|---|
| stream 单元测试总时长 | 62 个针对性 pytest 用例 wall time | 2.80s |
| 10k 字符事件累积 | `TurnAccumulator`，关闭网络和 DOM | 0.0175s，约 570k events/s |
| 50k 字符事件累积 | `TurnAccumulator`，关闭网络和 DOM | 0.0955s，约 523k events/s |
| runtime streaming 冷导入 | 独立 Python 进程，5 次 | 0.162-0.198s（不启用 tracemalloc） |
| 导入后 Python 工作集 | `Get-Process`，2s 快照，5 次 | 约 5.12 MB；该 Windows 沙箱的工作集数值仅作相对参考 |
| tracemalloc 当前/峰值 | 独立 Python 进程，导入 runtime streaming | 约 6.08/6.18 MB；启用追踪后导入 2.53-2.97s |
| cancel 返回时间 | `test_stream_cancel.py`，含固定 0.12s 触发延迟 | 1.13s call wall time |
| 文档构建 | `mkdocs build` wall time | 9.08s |

当前没有无 provider、无用户配置且能代表真实产品的“Agent ready”和“TUI mounted”端到端基准，因此 P0 不把网络/provider 初始化混入可重复门禁。第一 answer token 的 runtime 增量由同步 fake-agent trace 覆盖，DOM 刷新次数继续由 Textual sink 测试约束。P5 创建多个 SessionRuntime 后再测量每会话增量，并在 P8 确定内存预算。

## 4. 门禁命令

```powershell
uv run --no-sync pytest tests/test_runtime_streaming.py -q
uv run --no-sync pytest tests/test_stream_cancel.py tests/test_stream_tool_items.py -q
uv run --no-sync pytest tests/test_textual_stream_sink.py tests/test_turn_controller.py -q
uv run --no-sync ruff check .
uv run --no-sync pytest -q
```

## 5. 已知基线风险

- 当前 `stream_agent()` 语义解析主循环仍位于 `synapse.ui.stream`；P1 先建立 runtime 契约、累积器和 adapter，再迁移主循环，避免一次性重写成熟状态机。
- 当前 Textual worker 仍为 `exclusive=True`；这是 P3/P5 的目标，不在 P0-P1 提前开放并行。
- 当前 GoalService、MCP pool 等单例尚未项目化；P6 处理。
