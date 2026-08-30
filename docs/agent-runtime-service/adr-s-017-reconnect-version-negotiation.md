# ADR-S-017：S9 wire 版本协商与可恢复客户端

## 状态

Accepted（S9）。当前仍只有 wire v1。

## 决策

每条 WebSocket 连接可先调用 `runtime.protocol.negotiate`。参数严格为
`{"versions":[...],"client":{"name":str,"version":str}}`，其中 `client` 可省略；版本按客户端顺序选择。成功后连接锁定版本，重复相同协商幂等；业务请求在显式协商前沿用 S7 的 legacy implicit v1。无共同版本返回固定 `-32002/protocol_version_unsupported`，不触达 service；已开始业务后变更协商返回固定 `-32003/protocol_already_selected`。

协商结果固定包含 `wire_version`、`supported_versions` 与
`capabilities`。能力目前固定为 `legacy_v1`、`raw_cursor`、`watch_resume`。

`RuntimeWebSocketClient` 使用一个持久普通请求连接；watch 使用独立连接，避免
watch generation 与普通 pending response 互相干扰。请求 reader/writer 均有界，
查询和 artifact 请求可有限重试，命令在 frame 已发送而结果未知时绝不重放并抛出
`AmbiguousCommandError`。watch 保存 session raw cursor，断线后以该 cursor 和原 filter
重建 subscription；server subscription id 只在客户端内部使用。

watch lease 在同步构造点预留 active-watch slot，并在 enter 失败或 lease 退出时释放；每个 generation 先协商，再以最后一个成功入队的 raw cursor 和原 filter 重建 watch。事件、complete、error 必须匹配当前 generation 与 subscription id，旧 generation 的晚到 frame 被忽略。正常 complete 保留已接受队列尾部后 EOF；typed error（包括 replay gap、subscription error、协议错误和本地 overflow）清空尾部，恰好一次错误后 EOF。退出时仅 best-effort unwatch，不取消 session/turn。

## 安全与兼容性

客户端只接受严格 JSON、固定 envelope、固定 meta/capabilities；异常消息不包含 URI、
token 或底层异常文本。client 不导入 daemon/UI/ACP/service implementation，也不管理
service 或任何外部进程。
