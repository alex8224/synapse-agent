# ADR-S-011：S3 事件契约收口

- 状态：Accepted
- 范围：仅进程内 `LocalAgentRuntimeService` 事件 DTO、read/watch 语义；不引入网络协议、daemon、鉴权、Artifact 或消费者迁移。

## 决策

1. `EventFilter` 是 frozen/slotted 的纯数据值对象。`kinds` 只能包含当前 `TurnEventKind.value`，`turn_ids` 只能包含非空字符串；两维采用 AND，空集合表示不过滤。构造时复制并 canonicalize，服务端不会保存或修改调用方集合。
2. read 与 watch 在 raw `SessionEventEnvelope` 上使用同一个 matching helper，先匹配 kind/turn id，再投影 payload。未匹配事件不会触发投影、大小计算或 watch queue 计数。
3. 所有公开游标仍是原始 session sequence，绝不重编号。read 的 `limit` 是匹配事件返回上限；独立 `scan_limit`（默认 1024，范围 1..4096）限制一次最多扫描的 raw envelope。`EventPage.cursor`/`scanned_through` 指向最后扫描的 raw sequence，`has_more` 明确表示仍有未扫描 retained raw events。
4. watch 的 `EventStream.cursor` 是线程安全的只读 raw scan cursor。匹配事件投递后推进，不匹配事件在 ingress callback 中推进；服务端不保存 client cursor，重连仍使用 `after=stream.cursor.sequence` 与同一过滤器。
5. 对完整投影后的 `RuntimeEvent` 使用 canonical JSON（排序 key、禁止 NaN、紧凑分隔符、UTF-8）计算最终字节数。默认上限为 1 MiB，允许范围为 1 KiB..8 MiB。超限以 `event_too_large` 显式失败，不暴露 payload 值；read 不返回 partial page，watch replay/live 与 payload projection error 一样采用 first-terminal-wins、清理订阅、错误一次后 EOF、无 tail。
6. S3 的进程内有界性只有 read return limit + scan limit、watch queue_size、单事件 canonical JSON bytes 三条硬边界。不实现会静默丢匹配事件的 time token bucket，也不把 client filter 下沉到 broker retained `_delivery`。
7. filter 不改变 stale cursor 的 replay-gap 优先级：即使驱逐事件均不匹配，仍先报告 gap。replay+live 的原子订阅与顺序继续由现有 broker 保证。

## 后续边界

本 ADR 不定义 durable event log、网络编码、鉴权、Artifact 或消费者迁移；网络传输属于 S7，版本协商属于 S9，持久化/可靠投递不由本进程内游标承诺。
