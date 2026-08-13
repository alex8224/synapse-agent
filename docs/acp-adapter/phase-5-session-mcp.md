# P5：会话级 MCP Servers

> 状态：Complete（本地可验证范围）；配置转换、session-scoped pool、项目/Client 合并与冲突检测、startup rollback 和凭据隔离已有专项证据。真实 transport 端到端与取消竞态待 P8 外部门禁。  
> 前置条件：P4 门禁通过。  
> 后续阶段：P6 Client 反向服务。

## 1. 目标

完整处理 ACP 会话请求携带的 MCP servers，并保证配置、连接、工具和凭据严格按 session 隔离。

## 2. 数据流

```text
ACP mcpServers
  -> schema validation
  -> McpServerConfig conversion
  -> session-scoped MCP pool
  -> load tools
  -> build session Agent
```

## 3. 范围

- v1 基线要求的 stdio MCP。
- capability 声明的 HTTP 及 schema 中仍稳定的 transport。
- 项目 MCP 与 Client MCP 的合并、去重和冲突拒绝。
- load/resume 重连。
- close/delete/disconnect 释放。
- 异步 Agent factory 或等价非阻塞创建路径。

当前限制：仍需真实 stdio/HTTP/SSE MCP server 的端到端工具调用、项目 MCP 与 Client MCP
合并/冲突规则，以及取消/disconnect 竞争和跨 session 工具隔离的实测证据。

## 4. 安全约束

- Client MCP env、headers、URL credentials 不写日志、事件、trace 和持久化。
- command/cwd/env 经过安全策略验证。
- 同名不同配置不得静默覆盖。
- 一个 session 的 reload/close 不改变其他 session 工具集。

## 5. 门禁

- stdio MCP 工具可在创建它的 session 中调用。
- 其他 session 看不到该工具和凭据。
- 启动失败返回明确协议错误且资源全部释放。
- cancel/disconnect 与 MCP 启动竞争无泄漏。
- capability 与实际 transport 支持一致。

## 6. 风险与回滚

- 风险：现有 process-global pool 产生串线。缓解：禁止 ACP 路径依赖单槽 `get_active_mcp_pool()`。
- 风险：MCP 启动拖慢 ACP loop。缓解：异步创建并设置超时/取消。
