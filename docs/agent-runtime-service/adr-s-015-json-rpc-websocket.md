# ADR-S-015：S7 JSON-RPC/WebSocket 传输

- 状态：Accepted。
- S7 在 `synapse.runtime.transport` 提供可嵌入的 JSON-RPC 2.0 over WebSocket adapter；不启动 daemon，不迁移 CLI/TUI/ACP，也不读取 Settings、`.env` 或 token 文件。
- wire 版本固定为 `wire_version: "1"`，S7 不协商版本；协商与兼容矩阵留给 S9。
- 连接建立后先复制并只读暴露 headers 给 authenticator。成功得到 `Principal` 后，由 composition root 注入的 `service_factory` 绑定对应的 ACL service。transport 不接受 wire principal，也不授予 ACL。
- 输入严格限制为 JSON-RPC request object：仅 `jsonrpc/id/method/params`，params 必须 object，拒绝 duplicate keys、非有限数字、binary frame、超限或过深结构。单连接最多 32 个 inflight request 与 32 个 subscription。
- 输出使用 compact、sorted、UTF-8 JSON；response/error/notification 顶层带 `meta.wire_version`。每连接单一 bounded writer queue（默认 128，范围 1..4096）；overflow 关闭连接 1013，固定 reason，不静默丢消息。writer 发送失败关闭连接 1011，使用同一固定 reason，并完成所有待处理 acknowledgement。binary frame 关闭 1003，WebSocket message size 超限关闭 1009，认证或 service factory 失败关闭 1008。
- watch 使用两阶段 lease：先 `__aenter__` 检查 replay/gap 并获得 stream，成功 response 等待 writer acknowledgement、确认实际发送后才启动 pump。因此 replay/live notification 不会先于 watch response。unwatch 幂等，只关闭 subscription，不关闭 session 或取消 turn；断开连接同样只 detach watches。
- 正常 watch source EOF 产生一次 `runtime.subscription.complete`；terminal error 产生一次 `runtime.subscription.error`，不再 complete。
- service/router/manager 的 ownership 属于 S8 composition root；server.close 只停止 accept、关闭 connections 和 subscription leases，不 shutdown delegate。
- `attachments` 在 S7 wire 中不支持非空数组；空数组或缺省值仅为兼容占位。

## 固定业务方法

`runtime.session.open`、`runtime.turn.submit`、`runtime.turn.cancel`、`runtime.turn.steer`、`runtime.session.close`、`runtime.session.get`、`runtime.events.read`、`runtime.events.watch`、`runtime.events.unwatch`、`runtime.artifacts.stat`、`runtime.artifacts.list`、`runtime.artifacts.read`。

错误使用固定安全 message 和 `data.service_code`：parse `-32700`，invalid request `-32600`，method not found `-32601`，invalid params `-32602`，internal `-32603`，runtime service `-32000`，transport busy `-32001`。底层异常文本、payload、principal 和凭据不进入 wire。
