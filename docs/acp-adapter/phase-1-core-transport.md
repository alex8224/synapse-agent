# P1：核心传输与会话

> 状态：Completed  
> 前置条件：P0 门禁通过。  
> 后续阶段：P2 Prompt 完整语义。

## 1. 目标

建立独立、可测试、无 stdout 污染的 ACP stdio Agent，完成初始化、基础会话、prompt、cancel 和错误语义。

## 2. 设计

```text
synapse-acp -> acp.run_agent -> SynapseACPAgent
  -> ACPSessionRegistry -> RuntimeManager -> SessionRuntime
```

引入 `ACPSessionDescriptor` 固定 cwd、额外目录、MCP、Client capabilities 和 config，为后续阶段预留完整会话上下文。

## 3. 范围

- 独立 console script 和 server 生命周期。
- initialize/version/capability registry。
- `session/new`、基础 `session/prompt`、`session/cancel`。
- 同 session 排他、跨 session 有界并发。
- 未初始化、未知 session、重复 prompt、非法状态等错误映射。
- disconnect/shutdown 时取消与资源清理。

## 4. 非目标

- 除文本外的完整内容块。
- Permission resume。
- 历史加载和 Client-provided MCP。
- Client filesystem/terminal。

未实现的可选能力不得声明。

## 5. 门禁

- SDK Client 可通过 subprocess stdio 完成 initialize/new/prompt/cancel。
- stdout 中每一行均为 SDK 认可的 ACP 消息。
- 同 session 重叠 prompt 被拒绝，不同 session 可并发。
- cancel、disconnect 和 shutdown 不遗留活跃 turn 或 task。
- handler 不绕过 `SessionRuntime`。

## 6. 风险与回滚

- 风险：现有全局资源不适合多个 ACP session。缓解：P1 仅共享已验证安全的 model/checkpointer，并记录后续隔离项。
- 回滚：移除独立入口和 `synapse.acp`，不影响现有 CLI/TUI。
