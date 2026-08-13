# P6：ACP Client Filesystem 与 Terminal 服务

> 状态：Complete（本地可验证范围）；gateway、capability gating、filesystem/terminal scope、session-local tools、editor buffer 读取、session 隔离、能力组合和稳定错误已有专项证据。真实官方 Client 组合与跨进程竞态待 P8 外部门禁。  
> 前置条件：P5 门禁通过。  
> 后续阶段：P7 高级能力。

## 1. 目标

根据初始化协商结果，安全使用 ACP Client 提供的 filesystem 和 terminal 反向 RPC，并与 Synapse 本地 backend 形成统一工具语义。

## 2. 架构

```text
Synapse tools
  -> capability-aware backend
      ├─ ACP ClientServiceGateway
      └─ local backend fallback
```

## 3. 范围

- read/write text file。
- terminal create/output/wait/kill/release 的完整生命周期。
- 未保存 editor buffer。
- capability 缺失、RPC 失败和 Client disconnect 的降级。
- session-scoped terminal registry。
- workspace/additional directories 权限复核。

当前限制：未保存 editor buffer、统一 backend 语义、真实官方 Client 组合、RPC 失败/取消/断开
稳定错误和跨 session 竞态仍需补充；没有能力时继续使用本地 backend。

## 4. 约束

- capability 缺失时绝不调用对应 Client RPC。
- Client-backed 和 local backend 对模型暴露同一组工具名和结果语义。
- terminal output 有界读取，进程和句柄始终可释放。
- Client 路径仍受 Synapse deny/readonly/approval 约束。

## 5. 门禁

- 各种 Client capability 组合均有参数化测试。
- 未保存 buffer 可被 Agent 读取。
- terminal 正常、失败、取消、kill、disconnect 均无泄漏。
- 两个 session 的 terminal ID 和 filesystem scope 不串线。
- 无 Client capability 时现有本地工具行为不回归。

## 6. 风险与回滚

- 风险：backend 动态路由侵入 Agent assembly。缓解：先定义小型 capability-aware adapter，避免在每个工具内散落 ACP 判断。
- 风险：Client 与本地文件状态不一致。缓解：声明优先级并在 tool metadata 标记来源。
