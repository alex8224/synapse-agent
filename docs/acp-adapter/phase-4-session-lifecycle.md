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
- fork 后父子 checkpoint 独立。
- delete/close 不影响其他 session。
- 分页在并发更新下有明确、测试过的稳定规则。

## 6. 风险与回滚

- 风险：checkpoint saver 不支持原生 fork/delete。缓解：P4 先定义存储适配接口和事务边界。
- 风险：旧 session 缺少 cwd。缓解：显式 unknown 状态，不猜测。
