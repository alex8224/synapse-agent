# P2：Prompt、Content 与 SessionUpdate 完整语义

> 状态：Completed  
> 前置条件：P1 门禁通过。  
> 后续阶段：P3 Permission/HITL。

## 1. 目标

覆盖 P0 矩阵中 prompt 输入、流式输出、工具、计划、diff、usage 和 stop reason 的稳定语义。

## 2. 设计

- `ContentCodec` 负责 ACP ContentBlock 与 `UserTurn`/attachments 转换。
- `ACPEventBridge` 只消费 UI-independent `TurnEvent`。
- 缺失语义先增强 runtime event，不在 ACP 层解析 provider 原始 chunk。
- broker callback 经线程安全、有界队列进入 ACP asyncio loop。

## 3. 范围

- Text、Image、ResourceLink、EmbeddedResource 及 schema 中其他已声明输入。
- Agent message、thought、tool start/update/final、plan、diff、usage。
- 稳定 tool call ID 和严格状态转换。
- 所有稳定 stop reasons。
- delta 合并、背压、慢 Client 和顺序保证。

## 4. 数据约束

- base64 和 embedded resource 有大小上限。
- 不支持的 content type 在 capability 层禁用，并在误调用时明确报错。
- Tool args/result 结构化字段和 preview 分离，preview 始终有界。
- usage 只发送可准确计算的字段。

## 5. 门禁

- P0 ContentBlock 和 SessionUpdate 矩阵逐项有 schema 测试。
- 文本、thought、tool、plan、diff 顺序稳定且无重复终态。
- 慢 Client 不导致无界内存增长。
- 多 session 事件不串线。
- 现有 TUI/CLI 对扩展事件保持兼容。

## 6. 风险与回滚

- 风险：扩展 `TurnEvent` 影响现有 renderer。缓解：新增字段提供兼容默认值，先补 runtime 回归测试。
- 风险：文本合并改变可观察边界。缓解：只合并相邻同类 delta，不跨工具或终态事件。
