# P3：Permission 与 HITL 闭环

> 状态：Completed  
> 前置条件：P2 门禁通过。  
> 后续阶段：P4 会话生命周期。

## 1. 目标

将 Synapse/LangGraph 的 turn 级 interrupt 映射为 ACP prompt 生命周期内的 permission request，并在 Client 决策后透明 resume。

## 2. 状态机

```text
prompt running
  -> turn waiting_approval
  -> permission pending
  -> selected/rejected/cancelled
  -> resume turn
  -> running | waiting_approval | terminal
```

一个 ACP prompt 可以包含多个内部 turn，但只返回一次最终 `PromptResponse`。

## 3. 范围

- `PermissionCoordinator` 和 pending registry。
- interrupt action 到 ACP tool/permission model 的转换。
- approve、reject、cancelled 和会话级授权。
- 并行 actions 的稳定排序。
- prompt cancel、Client disconnect、超时和重复响应。
- resume 次数上限和异常恢复。

## 4. 一致性约束

- pending permission 必须归属 session、prompt、turn 和 tool call。
- cancel 后所有未完成 permission 都进入 cancelled，不能继续执行工具。
- permission RPC 失败不能默认批准。
- “approve for session”只作用于当前 session 和明确匹配的策略键。

## 5. 门禁

- 单次和多次 interrupt 均在同一 ACP prompt 内完成。
- approve/reject/cancelled 的工具执行结果正确。
- cancel 与 permission response 的竞争只有一个确定终态。
- Client 断开不会遗留图、future 或授权缓存。
- 现有 `/approve`、`/reject` 路径不回归。

## 6. 风险与回滚

- 风险：ACP 与 LangGraph 对并行审批粒度不同。缓解：P0/P3 明确顺序映射并参数化测试。
- 风险：恢复循环失控。缓解：设置有界次数并返回明确失败。
