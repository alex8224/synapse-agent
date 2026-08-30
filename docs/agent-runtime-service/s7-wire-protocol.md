# S7 Wire 协议表

## Envelope

| 类型 | 字段 |
|---|---|
| request | `jsonrpc: "2.0"`, `id: string/int`, `method`, `params: object` |
| success | `jsonrpc`, `id`, `meta: {wire_version: "1"}`, `result` |
| error | `jsonrpc`, `id`, `meta: {wire_version: "1"}`, `error: {code,message,data.service_code}` |
| notification | `jsonrpc`, `meta: {wire_version: "1"}`, `method`, `params`，无 `id` |

Request 不支持 notification。SessionRef 在所有方法中都是精确的 `{project_id,thread_id}`；两个字段均为非空、无 NUL、最多 256 UTF-8 bytes。未知字段、未知方法、principal 字段和非 object params 都拒绝。

## Methods

| method | params 的主要字段 | result |
|---|---|---|
| `runtime.session.open` | `session`, optional `command_id` | `OpenSessionResult` |
| `runtime.turn.submit` | `session`, `text`, optional `command_id/config_overrides/attachments` | `CommandReceipt` |
| `runtime.turn.cancel` | `session`, `expected_turn_id`, optional `reason/command_id` | `CancelTurnResult` |
| `runtime.turn.steer` | `session`, `expected_turn_id`, `text`, optional `command_id` | `SteerTurnResult` |
| `runtime.session.close` | `session`, optional `cancel_active/command_id` | `CloseSessionResult` |
| `runtime.session.get` | `session` | `SessionView` |
| `runtime.events.read` | `session`, optional `after/limit/scan_limit/filter/max_event_bytes` | `EventPage` |
| `runtime.events.watch` | `session`, optional `after/queue_size/filter/max_event_bytes` | `{subscription_id,cursor}` then notifications |
| `runtime.events.unwatch` | `subscription_id` | `{removed}` |
| `runtime.artifacts.stat` | `ref: {session,path}` | `ArtifactMetadata` |
| `runtime.artifacts.list` | `session`, optional `path/cursor/limit` | `ArtifactPage` |
| `runtime.artifacts.read` | `ref`, optional `offset/limit/expected_revision` | `ArtifactChunk` |

`filter` 是 `{kinds: string[], turn_ids: string[]}`；read 还接受 `limit`、`scan_limit`、`max_event_bytes`，watch 不接受 read-only 分页字段。artifact `data_base64` 原样传输。attachments 没有 S7 wire 编码：缺省或空数组合法，非空数组为 invalid params。

S9 增加 transport 控制方法 `runtime.protocol.negotiate`，严格参数为
`{versions: string[], client?: {name: string, version: string}}`。当前仍只有
`SUPPORTED_WIRE_VERSIONS=("1",)`；S7 客户端不协商时，首个业务请求隐式锁定 v1，保持
向后兼容。协商不属于 service dispatch，元数据仍使用连接选定的 v1。

## Limits and lifecycle

单 text frame 默认最多 1 MiB，binary frame 拒绝（1003）；超限 frame 由 WebSocket 层关闭 1009；JSON 最大 nesting 64，单连接最多 32 inflight/32 subscriptions。输出 canonical UTF-8、compact、sorted，单消息最多 8 MiB。单 connection 一个 bounded writer queue，默认 128，范围 1..4096；满时关闭 1013，固定 reason；writer 发送失败关闭 1011，使用固定 transport reason。watch response 在 stream enter 成功后等待 writer acknowledgement，实际发送后才启动 pump；replay gap 或 enter error 只返回 request error 且不残留 subscription。断开连接只 detach，不调用 close session/cancel turn。S8 daemon ownership、S9 client 重连/版本协商见 [S9 compatibility matrix](s9-compatibility-matrix.md) 与 [ADR-S-017](adr-s-017-reconnect-version-negotiation.md)。
