# Agent Runtime Service 架构决策记录

> 状态说明：`Accepted` 表示当前实施基线；发生变化时追加新决策，不覆盖历史原因。

## ADR-S-001：统一应用端口位于 runtime 之上，不替换执行栈

- 状态：Accepted
- 决策：`synapse.runtime.service` 是位于 `RuntimeManager`/`SessionRuntime` 之上的传输无关应用层；执行仍唯一经过既有执行栈，service 不直接实例化 `AgentTurnRuntime`、不调用 `stream_agent`/`agent.ainvoke`。
- 原因：既有 P0-P8 解耦已把执行、会话、事件与 UI 分离；统一服务应复用该栈，而不是再造一套执行路径。
- 影响：service 只能通过 `RuntimeManager.submit_ref`、`get_session_ref()` 与 `SessionRuntime` 公共方法访问运行态；禁止触碰 `_sessions`、`_lock`、`_async_runtime`。

## ADR-S-002：进程内优先，网络/daemon 明确延后

- 状态：Accepted
- 决策：S1-S6 只交付进程内实现（`LocalAgentRuntimeService`）；网络传输（S7）与 daemon（S8）之前不引入任何远程编码承诺。
- 原因：先证明统一 DTO/端口/游标语义在单一进程内正确（尤其事件不丢不重与 gap 检测），再考虑传输边界成本。
- 影响：`SubmitTurnCommand.attachments` 明确仅进程内兼容；端口以 Protocol 定义，未来网络实现只需实现同一 Protocol。

## ADR-S-003：客户端游标 = session sequence，turn-local sequence 独立保留

- 状态：Accepted
- 决策：所有公开事件游标使用 `SessionEventEnvelope.sequence`；`RuntimeEvent.turn_sequence` 保留 turn-local sequence 作为独立字段。
- 原因：跨 turn 的稳定游标必须由 session 层分配（ADR-005）；turn-local sequence 仍供调试与去重使用。
- 影响：DTO 同时携带 `sequence` 与 `turn_sequence`，二者语义不同，不允许混用。

## ADR-S-004：gap 由累计驱逐状态判定，而非 oldest_sequence

- 状态：Accepted
- 决策：`SessionEventBroker` 跟踪 `_dropped_through`（已驱逐的最大 sequence）；非负 cursor `< _dropped_through` 才判 gap。sequence=0 在未驱逐时合法；仅凭 `oldest_sequence=1` 不能判 0 为 gap。
- 原因：空 broker、cursor=latest、窗口内、preview 驱逐后的 stale cursor 都需要正确区分；窗口起点本身不能表达"是否丢过"。
- 影响：`read_after`/`subscribe_from` 原子返回窗口 + gap 标志；watch 在 gap 时不注册 live 订阅并抛 `replay_gap`。

## ADR-S-005：watch 采用 context-only lease + 有界 ingress，队列溢出即吸收终止

- 状态：Accepted（S1 硬化修订）
- 决策：`watch_events` 返回 context-only `EventWatch` lease（仅 async context manager，无 `__aiter__`/`__anext__`，从 API 结构上禁止裸 `async for service.watch_events(...)`）；`__aenter__` 才原子订阅并返回 `EventStream`（async iterator），`__aexit__` 确定性关闭。broker 回调线程先进入 `threading.Lock` 保护的有界 ingress，每 watcher 任意时刻至多一个 drain callback 经 `call_soon_threadsafe` 投递；溢出是吸收性终态：原子标记、清空 ingress/replay/live、关闭订阅，下一次 `__anext__` 恰抛一次 `event_overflow`，再下一次 `StopAsyncIteration`，绝不吐缓存尾部。
- 原因：旧实现每事件 `loop.call_soon_threadsafe` 使 asyncio ready queue 无界；`queue_size` 只约束 loop 侧 live 且溢出非终态，无法保证有界与确定性清理。裸 lease 迭代使订阅无法回收。
- 影响：replay 始终先于 live（overflow 除外）；source close 唤醒 blocked `__anext__` 且先按序消费已接受事件再 EOF；关闭 watcher 绝不关闭/取消 session；`queue_size` 严格验证 `1..4096`（`invalid_request`），非法值不静默修正。

## ADR-S-006：错误码稳定、子类化，未知底层错误原样上抛

- 状态：Accepted（S1 硬化修订）
- 决策：`RuntimeServiceError` 携带 `code`/`message`，为 `not_found`、`conflict`、`replay_gap`、`closed`、`invalid_session`、`event_overflow`、`invalid_cursor`、`invalid_request`、`invalid_event_payload` 提供子类。sessions 层新增 typed 异常（`SessionBusyError`/`RuntimeClosedError`/`InvalidEventCursorError`，均保留原 `RuntimeError`/`ValueError` 消息文本），service 只捕获这些 typed 异常并映射 `ConflictError`/`ClosedError`/`InvalidCursorError`；任何普通 `RuntimeError`（即使文本含 `closed`/`active turn`）原样传播。
- 原因：文本匹配分类会把低层故障误判为业务冲突（例如 "database closed unexpectedly"）；typed 异常让 manager 通过 `reserve_turn_or_raise()` 无 TOCTOU 地区分 closed 与 busy。
- 影响：`manager.submit` 用 `reserve_turn_or_raise()` 取代 snapshot 后 `reserve_turn()` 判空；错误映射集中在 `local.py` 的 submit/read/watch 边界，不依赖消息文本。

## ADR-S-007：事件 payload 严格 JSON 投影

- 状态：Accepted（S1 硬化新增）
- 决策：service 边界提供递归 JSON normalizer（read/watch 共用），`RuntimeEvent.payload` 使用显式 `JSONValue` 类型别名；输出保证 `json.dumps(dataclasses.asdict(event), allow_nan=False)` 成功。支持 None/bool/str/int/有限 float、Enum、dataclass、str-key Mapping、list/tuple、set/frozenset（稳定排序）、PathLike/UUID 字符串化、datetime/date/time isoformat、Decimal 字符串化、bytes 的显式 base64 tagged object（`{"$base64": ...}`）。拒绝非字符串 key、NaN/Infinity、循环引用、过深结构与未知对象（`invalid_event_payload`），绝不使用全局 `str/repr/default=str` 静默兜底。
- 原因：payload 必须可跨进程编码且确定可重放；静默兜底会掩盖 producer 契约错误。
- 影响：watch 的 replay/live 投影失败成为明确终止错误并清理订阅，不影响 Agent turn；投影结果是新对象图，不与 producer 共享可变结构。

## ADR-S-008：broker 关闭通知恰一次、在锁外回调

- 状态：Accepted（S1 硬化修订）
- 决策：`subscribe_from` 增加可选 `on_close`。broker 的投递统一由 `_delivery` 有序队列 + `_dispatching` 串行 drainer 承担：`emit()` 在 broker lock 内分配 sequence、保留 event、快照 subscriber 并按 sequence 把 `(envelope, records)` 入队 `_delivery`；唯一认领 `_dispatching` 的线程在锁外串行调用 callbacks（严格 sequence 顺序、可重入、无常驻线程）。`SessionEventBroker.close()` 在 close 线性化时拒绝新 emit、清空 registry、快照待通知的 `on_close` 集合（`_pending_close`，并发 `subscription.close()` 不能删除），并认领 drainer；drainer 先投递完队列中全部已接受事件，再在锁外对每个快照 record 恰一次调用 `on_close`——由此保证关闭前已接受的事件先投递到 subscriber callback，再触发 `on_close`（避免 EOF 越过已接受事件），不依赖任何 in-flight emit 计数。`close()` 幂等：重复 close 不重复通知。
- 原因：watcher 需要被阻塞的 `__anext__` 在 source close 时唤醒，且不能丢失关闭前已接受事件；回调若在锁内执行会与 broker API 重入死锁。
- 影响：`SessionSubscription.closed` 线程安全；event callback 内调 `broker.close()` 不死锁且关闭前事件不被 EOF 越过；close callback 重入 broker API 不死锁。

## ADR-S-009：broker 有序投递对 observer BaseException 的确定性恢复

- 状态：Accepted（S1 第六轮硬化新增）
- 决策：`SessionEventBroker._dispatch()` 对 subscriber callback 抛出的异常分三类处理，且所有退出路径都在 broker lock 内恢复 `_dispatching`，绝不让 drainer 永久占用、绝不静默丢失已接受 delivery 或 close notification：
  1. 普通 `Exception`：维持既有 observer failure isolation——继续投递后续 delivery，不传播给 emit caller。
  2. 非进程级 `BaseException`（`asyncio.CancelledError` 或其它自定义子类）：drainer 不停止——同一 envelope 的后续 subscriber records 与后续 queued delivery 仍按 sequence 严格投递，记录第一 `BaseException`；drainer 完成当前全部已接受 delivery（以及 pending `on_close`）后，把第一 `BaseException` 重新抛给认领 drainer 的 emitter/close 调用栈；同一次 drain run 中后续 `BaseException` 被第一者取代（不吞、不重复抛）。
  3. 进程级退出（`KeyboardInterrupt`/`SystemExit`）：终止当前 delivery（该 envelope 尚未调用的 subscriber records 放弃），立即重抛；`_dispatching` 恢复为 False，剩余 queued delivery 保留给后续 emit/close 认领的 drainer 投递；若 broker 已关闭时进程级退出逃逸，drainer 先完成剩余 queue 与 pending `on_close`（close work 绝不搁浅）再重抛。
- `_notify_closed` 对 `on_close` 采用同一策略：普通 `Exception` 隔离；非进程级 `BaseException` 记录第一者并继续通知其余 record；进程级退出立即中止剩余通知（通知集合已在 lock 内标记完成，不会重复触发）。外部 callback/`on_close` 仍在 broker lock 之外执行。
- 原因：旧实现 `_dispatch` 逐 callback 只捕获 `Exception`；callback 抛 `KeyboardInterrupt`/`SystemExit`/`asyncio.CancelledError` 时 `_dispatching` 永久为 True，后续 emit/close 不再启动 drainer，pending events 与 `on_close` 静默丢失。
- 影响：无常驻线程；并发/reentrant emit 仍按 sequence 严格有序，reentrant emit 只入队不递归。进程级 `KeyboardInterrupt`/`SystemExit` 原样穿透到认领 drainer 的调用栈，不被转换为业务错误（不宣称安全转换）。

## ADR-S-010：S2 生命周期命令采用 fenced、可线性化的进程内会话语义

- 状态：Accepted
- 决策：S2 提供四个生命周期 command——`open_session`、`cancel_turn`、`steer_turn`、`close_session`——以及对应的 frozen/slotted 纯数据 result；result 不暴露 runtime、handle、future、task 或其它执行对象。`cancel_turn` 与 `steer_turn` 必须提供 `expected_turn_id`，并在 `SessionRuntime` 的 session lock 内读取活动 turn、校验 fencing 条件并完成相应线性化操作。无活动 turn、turn id 不匹配、steering 不可用分别产生 typed 的 `NoActiveTurnError`、`TurnMismatchError`、`SteeringUnavailableError`；service 只映射已知 typed 错误，未知的 `RuntimeError` 原样传播。
- 决策：`open_session` 按 `SessionRef` 幂等；`command_id` 只用于关联请求与结果，不参与去重。manager 为每个 thread/ref 提供 lifecycle coordinator，串行化 open/create/register/close，使同 ref 并发 open 只调用一次 agent/session factory。close 完成后移除旧 generation，随后 open 可以创建新的 generation。
- 决策：`RuntimeManager` 是每线程 lifecycle coordinator；submit、close 与 open generation 在同一协调边界上线性化。`cancel_active=True` 的 close/shutdown 显式追踪 queued owner，取消其 semaphore acquire，并等待 submit cleanup 完成，以释放 reservation、submit lock 与其它资源；close 不得被全局 semaphore 阻塞。
- 决策：缺失 session 的 manager close 返回 `closed=False`（以及空的 turn/cancellation 结果），不视为错误。manager 的 strict close（`cancel_active=False`）对 claimed/settling conflict 直接失败且不改变状态；直接调用 `SessionRuntime.close` 保持 S1 兼容语义。`cancel_active=True` 的 close 等待 active future、persistence/goal settlement、permit 释放以及 submit lock 释放后才返回；并发 close join 同一个 close claim，broker close 恰执行一次。
- 决策：steer 的 queue listener 在 session lock 外派发。旧 turn settlement 在启动 goal follow-up 前，先在 session lock 内清理未消费 guidance，并在锁外派发对应 listener 通知，避免旧 turn 的 late steer 泄漏到 follow-up turn。
- 原因：生命周期控制需要在并发 open、close、submit、settlement 和 turn generation 切换时保持身份 fencing 与资源收敛；纯数据结果让进程内端口不泄露执行栈，typed 错误让调用方无需解析错误文本。
- 影响：S2 保证 command 的进程内线性化、close 的确定性 join/cleanup、旧 turn guidance 的 generation 隔离及 S1 兼容调用路径；命令本身不提供跨进程可靠投递或重试语义。
- 明确不包含：command-id 持久去重、网络传输、daemon 生命周期管理，以及持久化 event log。

## 决策更新模板

```text
## ADR-S-NNN：标题

- 状态：Proposed | Accepted | Superseded
- 决策：
- 原因：
- 影响：
- 替代方案：
- 重审条件：
```

## ADR-S-013：精确项目发现与多 RuntimeManager 路由

- 状态：Accepted
- 决策：S5 使用 frozen/slotted `RuntimeProject(project_id, workspace)` 作为
  service 内部项目描述；`project_id` 必须是真实字符串、非空、无 NUL 且有
  UTF-8 字节上限，workspace 同样只允许有界的非空字符串。`CatalogProjectProvider`
  只调用注入的精确 `get_project(project_id=...)`/lookup，不调用
  `resolve_project`，因此名称、前缀和路径不会成为运行时路由键。
- 决策：`RuntimeManagerRouter` 使用每项目 single-flight 条件事件构建一次
  manager；不同项目的构建不共享全局等待锁。descriptor 和 factory 返回值都
  进行精确身份校验；factory 错误原样传播且不发布半成品。已发布 generation
  不替换、不隐式驱逐，catalog 后续变化不影响该 generation。
- 决策：factory 若返回真实 `RuntimeManager` 但其 `project_id` 为 `None` 或与
  请求不匹配，该 manager 仍归 router 所有，登记在受线程锁保护的 rejected
  集合中；它不发布、不参与后续 single-flight 结果，并在 shutdown 中与已发布
  generation 一起关闭。无效的非 `RuntimeManager` 返回值没有可关闭的所有权，
  不执行关闭。发布集合与 rejected 集合按对象身份去重，保证一次 shutdown
  对同一对象至多调用一次 `shutdown()`。
- 决策：Local service 同时接受旧的裸 callable 与 router。所有端口在调用
  manager 前确认精确 `manager.project_id`；错误 manager 统一为 `not_found`，
  不调用其会话方法。裸 callable 的历史 unbound manager 首次成功路由绑定行为
  保留，router 路径禁止 `project_id=None` 的隐式绑定。
- 决策：DTO、routing、local service 及其默认 runtime/session 导入链保持传输
  无关且不触达 projects catalog、UI、ACP、transport 或 deepagents；routing
  采用窄导入，sessions 的公共行为保持不变。历史 persistence projection 不再
  作为 service routing 的惰性加载契约。
- 决策：router shutdown 原子进入 closing，之后解析抛 `RouterClosedError`，
  Local service 只将该 typed 异常映射为 `ClosedError(code='closed')`。shutdown
  只 snapshot closing 线性化瞬间的 inflight builds；由于 closing 后不能创建
  新 inflight，它等待这些 build 将成功 generation 发布或记录失败，然后再
  snapshot 并发关闭全部已发布与 rejected manager。build 成功必须先发布、标记
  state done，最后才让 resolver 观察到 `RouterClosedError`，确保 shutdown
  不遗漏该 manager。一个关闭异常不阻止其他关闭，重复调用 join 同一后台任务
  且不重复关闭 manager。调用者取消只取消该调用者的 join 观察，不取消后台
  收敛；后续调用者可继续 join 并观察同一个固定错误。
- 原因：项目 catalog 是发现投影而非运行时执行注册表；精确 id 与 immutable
  generation 防止前缀误路由、workspace 变化替换 manager 以及跨项目会话串线。
- 影响：S5 仍是进程内服务路由，不实现 S6 ACL、S7 network 或 S8 daemon。

## ADR-S-014：进程内 ACL 访问控制

S6 的 ACL、fail-closed wrapper、固定拒绝消息、scope 模型、watch snapshot 与
分层边界见 [ADR-S-014](adr-s-014-access-control.md)。

## ADR-S-011：S3 事件契约收口

- 状态：Accepted
- 决策：`EventFilter` 是 frozen/slotted 的不可变值对象；kind 仅接受当前 `TurnEventKind.value`，turn id 仅接受非空字符串，两维 raw metadata 匹配采用 AND。read 与 watch 共用 matching helper，并在匹配后才进行 payload projection、canonical JSON 字节计数与 queue ingress。
- 决策：所有游标继续使用原始 `SessionEventEnvelope.sequence`。read 的 `limit` 只限制返回匹配事件，独立 `scan_limit`（默认 1024，范围 1..4096）限制 raw 扫描；`EventPage.cursor`/`scanned_through` 指向最后扫描的 raw sequence，`has_more` 表示仍有 retained raw event 未扫描。stale cursor 仍优先产生 replay gap。
- 决策：`EventStream.cursor` 是线程安全、只读的 raw scan cursor；不匹配 callback 也推进它，服务端不保存 client cursor。replay+live 原子性与顺序继续由 broker 保证，重连由调用方使用 `after=stream.cursor.sequence` 和同一 filter 完成。
- 决策：完整投影 `RuntimeEvent` 使用 `json.dumps(dataclasses.asdict(event), sort_keys=True, allow_nan=False, separators=(",", ":"), ensure_ascii=False).encode("utf-8")` 计算最终字节数。默认上限 1 MiB，允许范围 1 KiB..8 MiB；超限使用脱敏的 `event_too_large`。read 不返回 partial page；watch replay/live 与 invalid payload 一样遵守 first-terminal-wins、清理 subscription、错误一次后 EOF、无 tail。
- 决策：S3 的进程内限流只定义为 read return limit + scan_limit、watch queue_size、per-event canonical JSON bytes 三条硬边界；不实现可能静默丢匹配事件的 time token bucket，不改 broker retained `_delivery`，不引入 durable event log 或网络协议。
- 影响：Artifact、网络/daemon、鉴权、消费者迁移留在后续阶段；网络属于 S7，版本协商属于 S9。

## ADR-S-012：只读 Artifact 端口以 session workspace 为 authority

- 状态：Accepted（安全审阅修订）
- 决策：S4 的 `stat_artifact`、`list_artifacts`、`read_artifact` 只服务于已存在的 `SessionRuntime`，并以该 runtime 的 `workspace` 作为唯一 workspace root；workspace 缺失、无效或非目录统一返回 `artifact_unavailable`。DTO 是 frozen/slotted 纯数据，文件内容以 base64 分块返回，绝不暴露 `Path`、文件句柄、runtime、backend 或 raw bytes。
- 决策：artifact path 是相对 POSIX 逻辑路径。绝对路径、Windows drive/UNC、反斜杠、dot segment、NUL、空路径和超长路径均拒绝；`ArtifactRef.path='.'` 对 stat/read 是 `invalid_artifact_path`，只有 `ListArtifactsQuery.path='.'` 表示 workspace root。解析后的 candidate 必须仍在 resolved workspace 内。最终 symlink 只有在 resolved target 留在 workspace 且 target 的相对路径也通过 ignore policy 时允许。
- 决策：所有 stat/read/list entry 均服从 `ToolIgnoreMatcher.from_workspace(root, extra_deny=session.settings.deny_fs_paths)`；`.git`、默认缓存、`.gitignore`、gitignore 与 deny 规则命中的对象统一 `artifact_forbidden`。policy 同时应用于请求逻辑 path 和 resolved target，防止 symlink alias 绕过。目录 listing 只读取直接子项，按 canonical POSIX path 排序；子项的损坏 symlink、逃逸、权限错误和不支持类型跳过，resolved target 命中 policy 的 alias 也跳过，requested directory 的问题显式返回。
- 决策：file revision 是基于 opened file `fstat` 的 `st_dev/st_ino/st_size/st_mtime_ns` SHA-256 token。read 严格限制 offset 与 1 KiB..1 MiB chunk，最多向 file wrapper 请求 `limit + 1` 字节；expected revision 不匹配、或 read 前后 revision 变化时返回 `artifact_changed` 且不返回数据。expected revision 有 UTF-8 字节上限；cursor 也以 UTF-8 字节数限长，并拒绝 malformed/non-ASCII 输入。resolver/open 间的删除、替换和逃逸统一映射为脱敏的 not-found/forbidden/changed 结果；Unix 在打开后通过 `/proc/self/fd` 再验 workspace 与 resolved policy，Windows 也复核 resolved candidate。
- 决策：list cursor 是带 session、directory、directory revision 和 last path 的不透明 base64url token，cursor 与目录或 revision 不匹配时报 `invalid_artifact_cursor`/`artifact_changed`。扫描最多 10000 个 entry，超限返回 `artifact_overflow`；分页在一次稳定扫描结果上 best-effort，目录并发变化返回 `artifact_changed`。
- 原因：应用端口需要将 session identity 与 workspace authority 绑定，避免把 host filesystem 或任意路径暴露给未来消费者，同时为网络/鉴权阶段保留稳定 DTO 与错误边界。
- 影响：S4 是进程内只读 workspace scope，不是用户 ACL；真正的用户鉴权/授权属于 S6。S4 不实现写入、上传、网络、daemon 或消费者迁移。
