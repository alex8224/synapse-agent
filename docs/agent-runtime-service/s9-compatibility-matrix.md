# S9 Compatibility Matrix

| Peer | Negotiation | Business before negotiation | Reconnect | Watch resume |
|---|---|---|---|---|
| S7 server | no | implicit v1 | client-side only | client-side cursor resume |
| S9 server/client | yes, v1 only | implicit v1 | bounded reconnect | raw cursor + stable filter |
| Future v2 peer | rejected | never selected | no downgrade | not applicable |

当前 `SUPPORTED_WIRE_VERSIONS` 精确为 `("1",)`；不声明或模拟 v2。所有 response、error
和 notification 的 `meta.wire_version` 都是连接选定的 v1。S9 client 的 watch lease
必须进入 async context 才建立连接，多个 watch 使用独立连接并受 active-watch 上限约束。

watch 重连次数有界且由 `max_attempts` 控制，backoff 使用注入策略。游标使用 session raw sequence；过滤器可使重连后的 cursor 跳跃，但每个 event 的 `sequence` 必须等于 notification 的 `cursor` 且严格递增。不匹配过滤器的 raw event 不假设会产生通知；本地队列 overflow 不静默丢事件，而是一次 `ClientEventOverflow` 后 EOF。
