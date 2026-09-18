# ADR-S-014：进程内 ACL 访问控制

- 状态：Accepted
- 决策：S6 在 Agent Runtime Service 应用端口外包裹一个 fail-closed ACL
  wrapper。`Principal` 由 composition root 在进入 service 前完成身份验证；它
  只携带不含凭据的 subject。ACL 使用 capability 与 project/thread scope 的
  精确匹配，多条 grant 采用 OR，不支持 wildcard、前缀、路径或大小写折叠。
  service 核心不导入 UI、ACP、transport、settings、projects 或 deepagents。
- 决策：wrapper 先验证最小 DTO shape，再从合法 `SessionRef` 提取 project 与
  thread，随后授权，最后才触达 delegate。拒绝统一为固定的
  `permission_denied`，不回显 subject、project、thread、capability、path 或
  其他资源信息，从而避免存在性 oracle。malformed DTO 使用既有
  `invalid_request`；authorizer 的 malformed context 使用
  `invalid_access_context`。
- 决策：`AclGrant.capabilities` 不允许为空；空 grant 没有可授权语义，构造时
  立即以不回显配置值的 `ValueError` 失败。`AccessControlledAgentRuntimeService`
  构造时要求 exact `Principal`/`AclAuthorizer` 类型，并对 delegate 做不调用方法
  的结构检查；非法绑定尽早以安全 `TypeError` 失败。`AccessRequest` 的 malformed
  context 继续以不回显值的 `ValueError` 失败。
- 决策：授权成功的 `watch_events` 返回 delegate lease，并采用 capability
  snapshot：之后替换外部 authorizer 不撤销正在消费的 stream；每次新 watch
  重新授权。lease 退出只关闭 subscription，不取消 turn、不关闭 session。
- 分层：S6 只保护应用端口，不读取 `settings.readonly`、
  `require_approval`、`deny_fs_paths`，也不调用 `runtime.safety` 或 ACP
  `PermissionCoordinator`。Agent tool safety 负责工具执行安全，ACP permission
  负责 ACP/HITL 协议层权限，两者与本进程 ACL 各自保持边界。
- 影响：本阶段仍是进程内授权；S7 才传输身份，S8 才引入 daemon 生命周期，
  S10 才迁移 CLI/TUI/ACP 消费者。
- 替代方案：在 manager/session 内分散检查会复制业务逻辑并产生资源枚举差异；
  传输层授权在 S6 范围外且不能保护本地应用调用。
- 重审条件：S7 定义远程身份、重放保护和 wire error 映射时，重新审视
  `Principal` 的边界，但不得让凭据进入 runtime service DTO。
