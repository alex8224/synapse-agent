# P7：配置、命令、Usage、Metadata 与认证

> 状态：Complete（本地可验证范围）；config、mode、thinking/approval、usage、title、available commands、providers（模型选择）、`_meta` 不污染和能力真值已有实现与专项证据。auth/logout、elicitation/NES/document 明确 not_target。  
> 前置条件：P6 门禁通过。  
> 后续阶段：P8 合规与发布收口。

## 1. 目标

实现 P0 矩阵中的稳定高级能力，使 ACP Client 能以协议原生方式管理 Synapse session 配置、命令、状态和认证。

## 2. 范围

- session config options。
- model、thinking、safety/approval 等设置映射。
- available commands 和动态更新。
- schema 保留的 mode/config 兼容接口。
- usage、context、cost 和 session info update。
- authentication/logout。
- 稳定 elicitation 和其他 Client 交互能力。
- `_meta` trace context 和受控 Synapse 扩展。

当前限制：ACP v1 适配层不声明 auth/logout、elicitation、NES、document
等没有安全产品语义或完整测试证据的能力；无法可靠计算的 context/cost 不发送。
`thinking` 直接映射 Synapse 全部级别 `off`、`minimal`、`low`、`medium`、`high`、`max`，
并应用到 session-local `Settings`，不会修改其他 session。
`providers/list` 暴露 `models.json` 的 profile（仅 `apiType`/`baseUrl`，不泄露 `api_key`），
`providers/set` 选择模型并重建会话。

## 3. 命令策略

- 只发布在 ACP 环境有意义的命令。
- `/theme`、`/select` 等 TUI 专属命令不发布。
- ACP 已有原生方法的 session 操作不重复为冲突 slash command。
- 命令仍作为普通 prompt 内容执行，除非协议提供专用方法。

## 4. 数据真实性

- 无法准确计算的 cost/context 字段不发送，不估造。
- 认证响应不包含 token、refresh token 或本地凭据路径。
- model/config 变化必须绑定当前 session，不能污染其他 session。

## 5. 门禁

- P0 高级能力矩阵逐项关闭。
- config 更新、事件通知和实际 Agent 行为一致。
- 命令列表不包含 UI 专属或无效命令。
- usage/metadata 可重复加载且不泄漏敏感信息。
- auth/elicitation 的 cancel、拒绝和断开均有测试。

## 6. 风险与回滚

- 风险：SDK 删除旧 model/mode 方法。缓解：以锁定 schema 的 config options 为主，兼容接口集中在 ACP 层。
- 风险：session setting 共享可变 `Settings`。缓解：建立 session-local settings snapshot/rebuild 策略。
