# P4：完整会话生命周期与历史

> 状态：Complete（本地可验证范围）；catalog、load/resume/list/close/delete、scope、ACP history projection、fork 独立性/回滚和非法转换门禁已有专项证据。真实 backend 并发竞态与跨域清理待 P8 外部门禁。  
> 前置条件：P3 门禁通过。  
> 后续阶段：P5 会话级 MCP。

## 1. 目标

按 P0 锁定 schema 实现全部稳定会话生命周期能力，并正确区分 new、load、resume、fork、list、delete 和 close 语义。

## 2. 设计原则

- Session metadata 与 LangGraph checkpoint 各自保持真源职责。
- load 回放历史；resume 不重复回放，具体以锁定 schema 为准。
- 历史使用 ACP 专用 projection，不复用 TUI view model。
- cwd、project identity 和 additional directories 都进入持久元数据。

## 3. 范围

- load、resume、list、fork、delete、close 等稳定方法。
- cwd 过滤和 opaque cursor 稳定分页。
- additional directories 及路径授权。
- SessionInfo 和 metadata update。
- checkpoint、goal、tool-output 等删除/复制边界。

当前限制：真实 checkpointer 的 parent/child 独立性、SessionStore/goal/tool-output 完整清理、
以及 close/delete/disconnect 并发竞态仍需真实 backend 和跨平台门禁。

## 4. 数据迁移

若 `SessionStore` 增加 cwd/project 字段：

- 迁移保持旧数据库可打开。
- 旧记录缺失 cwd 时不得错误归属到当前 workspace。
- cursor 不暴露数据库 offset 或敏感路径。

## 5. 门禁

- P0 会话方法矩阵全部有成功和非法转换测试。
- load 历史顺序、角色、工具和图片投影正确。
- fork 后父子 checkpoint 独立（子会话为独立终态 thread）。
- delete/close 不影响其他 session。
- 分页在并发更新下有明确、测试过的稳定规则。

## 5.1 fork 语义（轻量投影）

`session/fork` 采用**轻量投影**而非 checkpoint 深拷贝：

- 读取父 thread 的 messages，按 user 轮边界切分。
- 投影为**纯文本** user/assistant 消息：
  - 用户文本与 assistant 的可见文本逐字保留；
  - `tool_calls`、`ToolMessage` 结果与 reasoning/thinking **一律丢弃**，不内联
    工具调用摘要（把工具 JSON 当成模型"说过的话"会被模型模仿，导致行为异常）；
  - 只调用工具、无可见文本的轮次不产生消息。
- 通过 `CheckpointSeeder.seed_messages` 写入全新子 thread，并在 `END` 封口。
- 落盘后立即用 `TranscriptProjection.replace_from_messages` 重建子会话投影，
  否则历史读取（只读投影）会显示为空对话，而模型仍能从 checkpoint 回忆起来。
- 在子会话的 TUI 行记录血缘 `forked_from_thread_id` / `forked_from_boundary`
  / `forked_from_turn_id`（best-effort，缺失 TUI store 不影响 fork）。

不采用 `checkpointer.copy_thread`：安装的 `SqliteSaver` 不实现它，且它会把
DeltaChannel 祖先链逐条物理复制。轻量投影避免该膨胀，也使工具调用与结果的
配对校验不再成为约束（工具结果在投影阶段即被丢弃）。

血缘是只读的“出生证明”：写入后不随子会话演化更新；父会话删除后指针悬空属
预期（血缘用于溯源，不参与运行时）。

## 6. 风险与回滚

- 风险：checkpoint saver 不支持原生 fork/delete。缓解：P4 先定义存储适配接口和事务边界。
- 风险：旧 session 缺少 cwd。缓解：显式 unknown 状态，不猜测。
