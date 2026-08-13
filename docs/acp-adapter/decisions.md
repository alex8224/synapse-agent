# ACP 适配架构决策记录

> 状态：持续维护。  
> 实时实施状态见 [progress.md](progress.md)。

## ADR-001：使用官方 Python SDK

- 状态：Accepted
- 决策：依赖 `agent-client-protocol`，通过 `acp.run_agent` 提供 stdio 服务。
- 不采用：`deepagents-acp`、自研 JSON-RPC/schema。
- 原因：协议 schema 和 transport 应由官方 SDK 维护；Synapse 只负责自身 runtime 的语义适配。
- 约束：SDK 必须精确锁版本，升级先做 schema/API diff。

## ADR-002：目标是完整 ACP v1 Agent 语义

- 状态：Accepted
- 决策：P0 从锁定 schema 建立完整矩阵，P8 逐项关闭。
- 原因：允许阶段性交付，但不能把“最小可互通”误当最终范围。
- 约束：稳定方法、capability、ContentBlock、SessionUpdate 和错误语义都必须进入矩阵。

## ADR-003：ACP 是 runtime 的外部适配器

- 状态：Accepted
- 决策：`synapse.acp` 依赖 `synapse.runtime`，runtime 不依赖 ACP SDK。
- 原因：TUI、CLI、Web 和 ACP 应共享同一 headless runtime，不形成多套 Agent loop。
- 约束：ACP handler 不直接调用 Agent 原始 `astream()`。

## ADR-004：独立 stdio 入口

- 状态：Accepted
- 决策：新增 `synapse-acp` console script，不复用现有交互式 CLI 启动路径。
- 原因：ACP stdout 必须只包含有效 JSON-RPC 消息。
- 约束：日志、trace 和诊断只能写 stderr 或文件。

## ADR-005：capability 单一真源

- 状态：Accepted
- 决策：由 `CapabilityRegistry` 同时驱动 initialize 声明、handler guard 和测试参数化。
- 原因：避免声明支持但没有实现，或实现存在但永远不声明。
- 约束：capability 只能在对应阶段门禁通过后启用。

## ADR-006：一个 ACP session 对应一个隔离的 SessionRuntime

- 状态：Accepted
- 决策：session descriptor 固定 cwd、additional directories、MCP、Client capability 和 config。
- 原因：会话是执行、取消、权限和资源隔离域。
- 约束：同 session prompt 排他，不同 session 在全局上限内并发。

## ADR-007：Permission 在同一 ACP prompt 内闭环

- 状态：Accepted
- 决策：一个 ACP prompt 可以驱动普通 turn 和多个 LangGraph resume turn。
- 原因：Synapse 当前 HITL 以 turn 为边界，而 ACP permission 属于一个未结束的 prompt 生命周期。
- 约束：cancel 必须同时结束活跃 turn 和 pending permission requests。

## ADR-008：语义事件优先增强 runtime

- 状态：Accepted
- 决策：缺失的 tool args、diff、plan、usage 等先加入 UI-independent `TurnEvent`，再投影 ACP。
- 原因：禁止 ACP 层重复解析 provider/LangGraph 原始 chunk；TUI 也可复用增强语义。
- 约束：事件版本变化必须保持兼容或显式升级。

## ADR-009：线程边界使用有界事件桥

- 状态：Accepted
- 决策：broker callback 通过 `loop.call_soon_threadsafe` 投递到 ACP loop 的有界队列。
- 原因：Agent runtime 和 ACP server 可能运行在不同 asyncio loop/线程。
- 约束：终态和工具事件不得丢；文本可合并但不得乱序。

## ADR-010：Client-provided MCP 按 session 隔离

- 状态：Accepted
- 决策：ACP 请求携带的 MCP servers 使用 session scope pool，关闭 session 即释放。
- 原因：Client MCP 是会话资源，不是 Synapse 全局配置。
- 约束：不得持久化 Client MCP 凭据，不得泄漏到其他 session。

## ADR-011：Client 服务有能力时优先、无能力时回退

- 状态：Proposed，P6 确认
- 决策：filesystem/terminal capability 存在时优先 Client-backed backend，否则使用本地 backend。
- 原因：Client 能提供未保存 editor buffer 和 IDE terminal；本地 backend 保持无 Client 能力时可用。
- 待确认：同一 Agent graph 内 backend 动态路由的具体接口。

## ADR-012：协议版本基线由 P0 决定

- 状态：Accepted
- 决策：总体方案只固定 ACP v1 stable，不在调查阶段猜测具体 SDK/schema patch 版本。
- 原因：官方 SDK 变化较快，必须以实际可安装包、源码和生成 schema 为证据。
- 约束：P0 完成前不得提交依赖或实现代码。

## ADR-013：P1 先使用可注入 SessionFactory

- 状态：Accepted
- 决策：`SynapseACPAgent` 通过 `ACPSessionRegistry` 和可注入 `SessionFactory` 连接现有 `RuntimeManager`，默认 factory 只在实际 `session/new` 时构建 Agent。
- 原因：P1 协议测试不能依赖真实模型、API 凭据或 MCP；同时为 P4/P5 的 session descriptor 和资源隔离保留边界。
- 约束：生产默认路径复用 `build_coding_agent`，测试路径必须注入 fake factory；不得在 ACP handler 中直接调用 `astream()`。

## ADR-014：P1 不静默接受未实现的 MCP

- 状态：Accepted
- 决策：P1 handler 收到非空 `mcp_servers` 时返回明确的 invalid params；P5 实现 session-scoped MCP 后再启用。
- 原因：P1 尚未连接 Client-provided MCP，静默忽略会违反 capability/行为一致性。
- 约束：P1 可以接受空列表或省略字段；不得把配置写入全局 MCP 配置。

## ADR-015：notification 处理、batch 不适用

- 状态：Accepted
- 决策：notification（无 `id`）由官方 SDK router/dispatcher 处理，未知 notification 静默忽略、不产生响应；JSON-RPC batch 数组不纳入 ACP 适配范围。
- 原因：ACP wire 使用 newline-delimited JSON（每行一个 message），batch 数组不是该 framing 的合法输入；官方 SDK 亦按单条 message 处理。
- 约束：`session/cancel` 等 notification 必须保持幂等且不依赖响应；适配层不自行解析 batch，也不伪造 batch 支持声明。

## 决策更新规则

- 新决策追加 ADR，不覆盖历史结论。
- 被替代决策标记 Superseded，并链接替代 ADR。
- SDK/schema 升级、能力范围变化、状态机变化必须记录 ADR。
- 实时任务状态不写在本文件，统一维护于 `progress.md`。
