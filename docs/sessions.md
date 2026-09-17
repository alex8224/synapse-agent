# 会话管理

Synapse 使用 SQLite 存储会话检查点（checkpoint）和元数据。

## 会话存储

| 项目 | 默认路径 |
|---|---|
| 检查点数据库 | `.coding-agent/checkpoints.sqlite` |
| 会话元数据 | 与检查点同库 |

## 列出会话

```bash
synapse sessions list

# 限制数量
synapse sessions list -n 20

# 包含空会话（占位）
synapse sessions list --all
```

输出包含：thread_id、标题、模型、时间、消息数等信息。

## 删除会话

一个会话不只是一行元数据。它的对话同时存在于四个地方：checkpoint 数据库、transcript
投影（`transcript.sqlite`）、全文检索索引（`search-index.sqlite`）以及该会话的回滚快照
（`.synapse/turn-snapshots/<thread_id>/`）。只删元数据行的话，会话虽然从列表里消失，
却仍然能被关键字搜到（`search_session` 的全文分支只查检索索引，不查元数据表），也就是
「删了又冒出来」。

```bash
# 删除一个会话：记录 + 全部对话历史，不可恢复
synapse sessions delete <thread_id>
```

Web 控制台里的「删除」走同一个 `runtime.session.delete`，行为一致；两者的结果都会报告
是否真的清干净了（有存储拒绝时会点名，可用下面的命令补清）。

### 清理遗留的孤儿历史

在「删除只删元数据」的版本下删掉的会话，其对话仍留在磁盘上。扫描并清除这些没有元数据行
的 thread：

```bash
# 只列出（默认，dry run）
synapse sessions purge

# 真正清除
synapse sessions purge --apply
```

扫描以元数据表里的 thread 列表为前提：元数据文件缺失或读不出来时**不会**列出任何候选
（否则每个会话都会被当成孤儿删掉）。已经在列表里的会话永远不是候选。

## 恢复会话

通过 `--thread-id` 选项恢复之前的会话：

```bash
synapse tui --thread-id <thread_id> -w .
synapse run "继续刚才的工作" --thread-id <thread_id> -w .
```

TUI 模式下也可以在界面内浏览和切换历史会话。

### TUI 恢复会话的分页加载

TUI 启动恢复会话时从独立的 `transcript.sqlite` 轻量投影读取最后
`history_tail_turns` 轮（默认 20 轮，可在配置中覆盖）。它不会长期保留
完整 LangChain 消息列表；token 汇总也从投影 O(1) 读取。旧会话第一次打开时
会由一次性子进程从 checkpoint 兼容回填投影；子进程退出后，完整历史的临时
反序列化内存会由操作系统直接回收，不会抬高长期运行 TUI 的内存水位。后续
打开和翻页只读取所需 turn 范围。

活跃会话中新完成的 turn 也遵循同一个可见窗口。超出窗口的 Textual widgets
会从 DOM 卸载，内容仍保存在 checkpoint 与 transcript 投影中；滚动到顶部时
按需从投影重新加载，避免长时间运行时界面对象和完整正文无界增长。
更早的历史通过滚动分页加载：

- 启动后自动滚动到 transcript 底部，显示最近 N 轮。
- 滚动到 transcript 顶部时，异步加载更早的一批历史并插入到当前内容上方，
  滚动位置保持不变。
- TUI 最多保留 5 个已挂载历史页；继续向前翻页时会卸载最远的新页面及其
  Widget 业务引用，避免 DOM 和 Python 对象随会话长度无界增长。
- 切换会话（`/session`）或重新加载后，分页状态随 transcript 一起重置。

## Web 控制台：会话列表与历史 RPC

Web 控制台（`web/`）通过 Agent Runtime WebSocket 的 JSON-RPC 读取会话列表与
历史投影，不再使用 Vite 开发中间件直读 SQLite（`/api/sessions`、
`/api/session-transcript` 及 `get_transcript.py` 生成逻辑已移除）。两个
方法都**只能在 negotiate 完成后的同一连接上调用**；列表读取时机在
`runtime.protocol.negotiate` 成功之后。

### `runtime.session.list`

请求 `params`：`{project_id, limit=50, offset=0}`。

- `limit` 上限 100（默认 50），`offset` 上限 100000；非法值返回
  `invalid_params`。
- 结果 `{items: [{thread_id, title, model, active_model, created_at,
  updated_at, summary}], next_offset: number|null, total}`。
- 按 `updated_at DESC, thread_id` 排序；`next_offset` 为
  `offset + items 数`（仍小于 `total` 时为数字，否则 `null`）。
- 会话元数据库缺失或没有 `sessions` 表时返回**空页**
  （`items: [], next_offset: null, total: 0`），不创建文件、不迁移 schema。
- 只读且限制大小：单条 ≤ 64 KiB、单页 ≤ 256 KiB（线格式）；超限抛
  `HistoryTooLargeError`，不会静默截断字段。
- 权限：`project_id` 必须对应已注册项目；读操作全部在单个只读事务内完成。
- 冷启动不要求先 `open` 会话：对已注册项目，进程按需解析其
  descriptor/settings（构建轻量 manager generation）即可列表，不构建
  agent、不打开会话；daemon 冷启动且 Web 先 list 时也能读取已有会话元数据。

### `runtime.session.history`

请求 `params`：`{session: {project_id, thread_id}, before_turn: null|int>=1,
limit=20}`；`limit` 上限 100（默认 20），`before_turn` 必须 ≥ 1 或 `null`。

- `before_turn: null` 取**最新**的 `limit` 轮；否则
  `end_turn = min(total_turns, before_turn - 1)`，即严格早于
  `before_turn` 的窗口。
- 结果
  `{events: [{kind, text, tool_calls: [{id, name, args}], tool_results:
  [{id, name, content, status}]}], start_turn, end_turn, total_turns,
  has_more, available}`。
- `events` 按 `event_seq` 升序；页内每轮以 `user` 事件开头，
  `kind` 为 `user | answer | thought | tools | meta`。
- `has_more = start_turn > 1`；Web 用 `before_turn = 当前最旧页 start_turn`
  继续往前翻页，且只有 `has_more === true` 时才允许加载更早历史。
- **`available: false`** 表示该会话没有 transcript 投影（旧数据、从未在
  投影感知的 runtime 中打开过等）。这不是“空历史”：Web 明确提示不可用，
  不渲染为空历史、**不回退** checkpoint。`available: true` 且
  `total_turns == 0` 才是真正的新会话空历史。
- 页级安全上限：单页事件 ≤ 4096 条、payload 原文 ≤ 896 KiB、页面线格式
  ≤ 896 KiB（都在 1 MiB 传输帧预算内，留 128 KiB 余量）；投影 JSON
  非有限数值/损坏会报错而不是静默吞掉。分页按**轮**切分，最小页即一轮，
  因此上限必须容得下**单个**内容密集的轮次（大工具输出、整文件读取），
  否则任何页大小都取不出该轮。
- 权限：`project_id/thread_id` 必须是当前 runtime 可路由的会话引用；
  transcript 库不存在或缺表同样返回 `available: false`。
- 冷启动读取与 `runtime.session.list` 相同：已注册且可路由的项目无需先
  `open` 也能读历史；解析只构建轻量 manager（settings），不构建 agent、
  不打开会话。未知项目仍返回 `not_found`，关闭后的 runtime 返回
  `closed`。

### 投影历史 ≠ 完整 checkpoint

两个 RPC 只暴露**结构化投影**，不暴露 LangChain 消息、精确事件序号或
turn id，也不会反序列化完整 checkpoint。投影按轮追加，活跃 turn 的投影
可能落后于实时事件 broker，因此“历史 + 实时续接”不是原子快照：

- 首次 attach 时 Web 从 `open.view.latest_sequence` 之后开始 watch（不整段
  replay，避免与已渲染历史重复）。
- 历史页加载期间到达的实时事件先进入临时 buffer，快照应用后再合并到消息
  尾部，**不覆盖历史、不丢事件**；但没有承诺跨“投影快照 ↔ 实时流”边界的
  严格逐字节原子一致。
- 切换会话时用 `(project_id, thread_id) + generation` 守卫，旧的异步
  open/history 响应不会覆盖新会话；不会自动翻完所有页（无界加载）。

### Web 控制台断线恢复契约（第四阶段）

Web 控制台客户端（`web/src/client/SynapseRuntimeClient.ts` + store）对运行时
连接丢失实施**有界恢复契约**。恢复判定在第四阶段 A 切片接通正式对账快照
（`runtime.session.reconcile`，见下节）后依然向后兼容：支持快照的 server
按 epoch/cursor/durable 覆盖决策，旧 server 自动降级为下述既有游标语义：

- **连接 generation 围栏**：每次建连递增 generation，响应、`runtime.event`
  通知与 close 帧都按发送它的 socket 围栏。被替换的旧 socket 的晚到帧不能
  解析新连接的 pending 请求，也不能作为实时事件污染当前会话。
- **有界退避重连**：仅在健康 connect 之后 arm；意外断开按
  `maxAttempts`（默认 5）/指数退避重连，预算耗尽进入可观测的 `error` 终态，
  不会无限重连。`disconnect()` / `closeRuntime()` 是用户主动关闭：取消预算，
  后续不再自动重连。重连状态通过 `onRecovery`/`connectionState` 暴露。
- **游标续 watch，绝不静默 after=0**：客户端按 `runtime.event` 的 `cursor`
  跟踪最后投递序号。重连成功后若仍是同一 session，则用该游标重新
  `watchEvents(after=lastCursor)`，服务端 replay 恰好补上断线期间遗漏事件，
  不漏不重。仅当从未投递过任何事件时才退回 `open.view.latest_sequence`。
- **epoch 围栏（A 切片）**：attach 时经 `reconcile_session` 记录 broker
  `live_epoch` 基线。重连续接前先取新快照：epoch 变化（会话重开 / daemon
  重启）、游标低于保留水位或高于流末尾 ⇒ 进入 `resync`（全量重同步），绝不
  用旧游标静默续接新 broker；active turn 前缀被逐出 ⇒ 进入 `incomplete`，
  直至结算后按 durable 补全，**不冒充无损**。
- **显式 gap / 全量重同步**：游标超窗（`replay_gap`/`invalid_cursor`）或
  订阅被 `event_overflow` 终止时，store 进入可观测的 `resync` 态并**从正式
  历史快照全量重同步**（`runtime.session.history` 最新页 +
  `open.view.latest_sequence` 之后 watch），而不是用 `after=0` 冒充成功。
- **重复抑制**：attach 快照 `probe[]` 已判定 `covered`（持久化赢过重连竞争）
  的 turn，其缓冲中的 live 回放被丢弃——历史页会渲染整轮，重放即重复内容。
- **typed 错误保留 service_code**：RPC 错误带 `service_code`
  （`replay_gap`、`event_overflow`、`invalid_cursor` 等），调用方据此区分
  “显式缺口”与一般传输错误；不再把服务端错误降级成无差别 message。
- **订阅终止通知**：`runtime.subscription.complete`/`runtime.subscription.error`
  被客户端解析并经 `onSubscriptionNotice` 暴露；detach（unwatch/断线）与
  session cancel 语义保持分离，断线不会取消 daemon 中仍在运行的 turn。
- **事件缓冲有界**：历史页加载期间到达的实时事件缓冲有上限
  （`MAX_LIVE_BUFFER`），超出后丢弃最旧事件并累计可观测的
  `liveBufferDroppedCount`，避免无界内存增长。
- **提交结果未知可观测**：submit/steer 请求已发送但连接在回执前断开时，
  客户端把该请求标记为 `unknownOutcome`（`ConnectionLostError`），store 置
  `recoveryState='unknown'`，**不会盲目重发**（避免重复工具执行）；用户经
  会话状态查询确认结果。

`replay_gap`、订阅 error/complete、watch after 游标等语义在 S7/S9 已存在并
由 Python 客户端测试覆盖；A 切片**新增** `runtime.session.reconcile` 一个
只读 RPC 方法（见下节）供正式恢复判定消费，未改动其它字段/方法。历史投影 ↔
实时流的严格原子一致仍**未承诺**（见上节）。

## Headless / daemon 会话持久化写入（第四阶段 B 切片）

`RuntimeManager` 一直支持 `persist_result` 钩子，但 daemon 与
`LocalProjectRuntimeConsumer` 组装时没有注入，导致 headless
（`synapse run`/ACP）与 runtime daemon 执行的回合**从不写**
`transcript.sqlite` 或 session 元数据；只有 TUI 本地路径会写。B 切片接通两条
正式组装路径，复用**中性领域逻辑**（`synapse.runtime.sessions.persistence`）
而非 UI 控制器：

- **单个 writer**：`LocalProjectRuntimeConsumer`（CLI/ACP/TUI 服务会话共用）
  与 daemon 的 `RuntimeManager` 构造时注入
  `synapse.runtime.sessions.persistence.RuntimeProjectPersistence`。该 binder
  惰性打开项目 `SessionStore` + `TranscriptProjection`（路径沿用
  `settings.resolved_sessions_path()` 与 transcript 同目录规则），回合终态经
  服务 `persist_result` → `SessionPersistence.persist`（transcript 追加 +
  summary + 可选 catalog），session 元数据行经 `SessionStore.touch` 写入。
- **禁用策略，不凭空改默认**：`checkpoint_backend == "memory"` 或
  `resolved_sessions_path` 不可解析时 binder 为 `enabled=False`，不创建文件、
  `persist_result` 是有界 no-op；默认 `sqlite` 路径保持现有默认解析规则。
- **幂等/去重**：transcript 按 `turn_id` 去重（重复 settle 同一 turn 不追加、
  不重复累计 usage）；`cancel`/`failed` 终态按 `SessionPersistence` 既有策略
  保留部分输出一次；审批 resume 只累计 usage、不重复追加用户 turn。
- **失败可观测**：写入异常经 `SessionRuntime._settle` 记入
  `view.last_error`（不吞异常、不虚报持久成功）；`RuntimeManager.shutdown()`
  在会话结算后关闭 `persist_resources` 恰好一次，关闭失败会抛出可观测错误。
- **资源所有权**：资源随 manager 生命周期关闭（consumer.close /
  router shutdown → manager.shutdown），订阅 detach 不取消 session，
  watch/`close_session` 语义不变。
- **历史/实时水印对齐**：B 只接通"回合结束可经正式 service list/history
  读到、重建 runtime/daemon 后仍可读"；历史投影 ↔ 实时流的服务端
  epoch/revision 对齐是第四阶段 A 切片（下一节），由 A 的服务端对账快照
  提供，wire/客户端消费在 A 之后继续推进。

## 历史/实时恢复对账（第四阶段 A 切片：服务端快照）

第四阶段 A 的目标是"可靠 history-live 恢复契约"。第一块落地的服务端可验证
子切片是**只读对账快照**（`runtime` 域方法 `reconcile_session`，域 DTO 在
`synapse.runtime.service.recovery`）。它不假装跨 SQLite/内存原子，而是把
两套独立存储各自的边界一次暴露给恢复方，并显式标注"数据不足"，禁止用
`after=0` 或历史尾页冒充恢复成功。

### 背景：两套存储没有共享水印

- transcript 投影是持久 SQLite（按 `turn_seq`/`event_seq`，turn 只在结算时
  追加）；事件 broker 是会话内存在档（按 broker `sequence`，重启/重开会话
  会新建 broker，sequence 从 0 重新计数）。
- 两者唯一可跨存储对齐的持久身份是 `turn_id`（`transcript_turns` 表只保存
  `(thread_id, turn_id)` 集合，无顺序水印）。
- 因此"历史 + 实时续接"**不是**一次快照里可以声称的原子状态。快照只提供
  判定所需的事实；收尾的窗口竞争由客户端在快照后立即续接 watch 闭合。

### 只读快照字段与语义

`reconcile_session(session, probe_turn_ids?)` 要求会话**已打开**（不隐式
open、不建 agent、不 cancel turn），返回（bounded，见下）：

| 分组 | 字段 | 语义 |
|---|---|---|
| durable | `history_available` | transcript 投影是否存在（`false` ≠ 空历史） |
| durable | `history_total_turns` | 已结算持久轮数（覆盖边界） |
| durable | `probe[]` | 请求的 ≤32 个 `turn_id` 在 `transcript_turns` 的成员判定 |
| live | `live_epoch` | 当前 broker 实例身份（重开会话/daemon 重启必变） |
| live | `live_latest_sequence` / `oldest_sequence` / `dropped_through` | broker 保留边界 |
| live | `active_turn_id` | 运行/结算中的 turn（未持久） |
| live | `latest_turn_*` | broker 最新观测 turn 的首发序号、可重放起点、`intact` |

关键判定（客户端据此恢复，全部有明确信号、无静默丢失）：

- **游标跨重启（碰撞）**：`live_epoch` 变化 ⇒ 旧游标属于别的流实例，即使其
  数值落在新 sequence 区间内也**不得**续用（新 broker 的 seq 1..N 会被旧游标
  静默跳过）；先按 durable 覆盖重同步。
- **重复补播**：live 事件所属 `turn_id` 的 probe 已 `covered=True` ⇒ 该轮已
  持久，渲染方按轮去重，不再把 replay 当新内容追加。
- **active turn 尚未持久**：运行中的 turn probe 恒 `covered=False`、计入
  `latest_turn_*`；只有结算写入后才翻转为 `True` 且 `history_total_turns` 增。
- **前缀/窗口缺口**：`latest_turn_intact=False`（该 turn 有事件被保留窗口
  逐出）或游标低于 `dropped_through` ⇒ 不能声称从 turn 起点无损重放；该轮
  最终内容以结算后的 transcript 为准，之前显式 `incomplete`/等待结算，绝不
  伪造"已补全"。
- **persist/reconnect 竞争**：客户端离线期间结算落库后，下次快照的 probe
  翻转为 `covered=True`、`history_total_turns` 增大 —— 竞争结果以 durable
  覆盖为准，不靠猜。

限制与保证（如实声明）：

- **不承诺原子**：live 快照与 durable 读不是同一瞬间；两读之间可能发生
  结算。快照是"前置条件"，客户端必须随后立即续接 watch/在覆盖变化时按
  probe 重对账。
- **有界**：`probe_turn_ids ≤ 32`、每个 ≤ 256 字节、重复折叠；结果不含
  事件正文、无分页，一次往返。
- **ACL 先读**：按 `session.read` 先鉴权；旧 delegate（无 `reconcile_session`）
  可构造，调用报确定性 `InvalidRequestError("session recovery is
  unavailable")`，不会在构造期炸掉整个 wrapper。
- **服务端可验证**：离线测试（真实 service + 临时 SQLite + 受控 turn runtime
  + broker 事件注入）覆盖五种竞争/缺口，见
  `tests/test_runtime_recovery_s4_a.py`。

### transport RPC 与客户端接线（A 切片完成）

对账快照以**新 RPC 方法**暴露，旧 negotiate 请求/响应形状与 `CAPABILITIES`
精确集合**原样保留**（不加能力位、不加响应字段）：客户端在 negotiate 成功后
才能发任何业务帧，reconcile 只在已协商连接上调用。

- 方法：`runtime.session.reconcile`，请求 `params`：
  `{session: {project_id, thread_id}, probe_turn_ids?: string[]}`；
  `probe_turn_ids` ≤ 32、每条 ≤ 256 字节、重复折叠，非法值回
  `invalid_params`。结果即 `SessionRecoverabilityView` 的精确 wire 投影
  （durable 覆盖 + live epoch/保留边界 + probe[]）。
- 权限/旧 delegate：wire `dispatch` → ACL 包装器按 `session.read` **先鉴权**；
  delegate 缺 `reconcile_session` 时回确定性
  `invalid_request`（"session recovery is unavailable"），不炸构造期。旧 wire
  server（方法白名单无 reconcile）回 `method_not_found`。
- Python 客户端（`RuntimeWebSocketClient.reconcile_session`）：严格解析
  epoch/cursor/status 全部字段；旧 server/旧 delegate 分别以
  `method_not_found`/`invalid_request` 呈现为 `RecoveryUnavailableError`
  （**明确不支持**，不 `after=0`、不假无损）。
- TS/Web：`SynapseRuntimeClient.reconcileSession` 严格解码；
  `useConsoleStore` 在 attach/reconnect/gap 用快照决策（epoch 变化、游标超
  窗、active 前缀不足 → `resync`/`incomplete`），attach 快照 `probe` 用于
  重复抑制；旧 peer 自动降级既有有界游标语义。
- 服务端可验证：见 `tests/test_runtime_recovery_s4_a_wire.py`（协议 decode/
  dispatch 边界、旧 delegate、旧 wire server，以及真实 Local service + 临时
  SQLite + 注入 broker + localhost WS 的纵向测试）。纵向部分额外证明
  **快照→watch 无缝窗口**：客户端取一个 reconcile 快照后，立即按
  `live_latest_sequence` 开 watch，能恰好收到下一条 live 事件（前缀不重放、
  无缺口、无重复）；active turn 前缀被保留窗口逐出时，快照报告
  `latest_turn_intact=False` 且从旧游标开 watch 得到类型化 `replay_gap`
  错误——服务端不会用 `after=0` 假恢复。TS 见 `web/tests/reconcileDecider.
  test.ts` 与 `web/tests/runtimeClientReconcile.test.ts`。

本轮纵向测试证明的服务端边界（如实声明）：

- 快照之后**立即续接** watch 是无缝的（单连接单 broker 内验证）；
- 逐出导致的 `replay_gap` 是类型化 wire 错误，不是静默成功；
- 但服务端**不承诺**跨进程/多客户端共享同一 broker 快照的原子一致，也不
  承诺任意时刻取快照后"所有已发生事件都在保留窗口内"——窗口竞争由客户端
  按 probe/durable 覆盖在快照后继续闭合（见上文"不承诺原子"）。

当前边界（本轮 A 剩余范围之外，勿当作已完成）：第五阶段 GUI/部署、bootstrap
凭据、scratch 处理未在本轮实现；服务端历史投影 ↔ 实时流的严格原子一致仍
**未承诺**（快照是前置条件，客户端随后立即续接 watch/按 probe 重对账）。

### `runtime.config.get`（只读运行时配置）

Web 控制台通过 RPC 读取**只读**的当前运行时配置，不再使用 Vite
`/api/runtime-config` 开发中间件直读 `get_runtime_config.py` 的输出。该 RPC
只暴露白名单显示字段，绝不会把完整 `Settings` 序列化到线上：

请求 `params`：`{session: {project_id, thread_id}}`。

- 权限与 `runtime.session.history` 相同：按 `session.read` 先鉴权再读取；
  未知项目返回 `not_found`，关闭后的 runtime 返回 `closed`。
- 结果字段固定为：`current_model`、`available_models`、`thinking_level`、
  `thinking_levels`、`mcp_servers`、`mcp_enabled`、`can_set_thinking`、
  `can_toggle_mcp_global`。`can_set_thinking` / `can_toggle_mcp_global`
  当前恒为 `false`，Web 将对应控件渲染为只读并提示原因。
- `mcp_servers` 每项只含 `name` / `transport` / `enabled` / `tool_prefix`。
  **不会**返回 `command`/`args`/`env`/`url`/`headers`/API key 等敏感字段，
  也不会返回 active goal 或 attached 等伪造状态。
- 已打开的会话优先读其 session-bound settings；未打开的会话读项目
  settings。该查询只做纯读取：不构建 agent、不打开会话。模型列表/推理等级/
  MCP 服务器数量与文本有安全上限，超限时报 `config_overflow`，不静默截断；
  registry / MCP 配置的核心读取错误不会被吞掉。

写范围保持不变：会话模型切换仍走 `runtime.session.rebind`（单模型 rebind），
单台 MCP 服务器开关仍走 `runtime.session.mcp.reload`。全局 MCP 开关与推理
等级**没有**真实写路径，Web 不再向任何 `/api/runtime-config` 端点 POST。
`web/get_runtime_config.py` 文件本身保留但 Vite/Web 不再引用它提供运行时
配置。

## Web 控制台正式宿主与配对认证（第五阶段收口）

浏览器控制台不再依赖 Vite 开发中间件直读 SQLite/token：生产路径由正式宿主
`synapse-web-console`（薄 aiohttp 进程）提供静态产物、配对/会话 API 与
`/runtime-ws` WebSocket 中继，详见 `docs/web-console/formal-host.md` 与
`docs/web-console/index.md`。

- **配对契约**：会话 cookie 只在 `POST /api/pair`（体 `{"code":"XXXXXXXX"}`，
  配对码由宿主启动时打印到 stderr、单次使用、默认 300s 过期）成功后签发，属性
  为 HttpOnly、SameSite=Strict、Path=/；`GET /api/session` 查询当前会话，
  `POST /api/logout` 作废全部会话。`GET /api/bootstrap` 已删除，恒返回 405 且
  不签发 cookie。响应只含项目上下文（`project_id`、`workspace_path`、名称、
  git 分支），**不含** daemon bearer/env/model 秘密；前端严格校验并只复制白名单。
  另有一个只读诊断端点 `GET /api/runtime-status`（需有效会话 cookie + `Host` 允许表；
  返回 daemon 端点、state dir 与 `start synapse-runtime` 提示，响应不含 token；
  `POST` → 405）。
- **WS 中继**：浏览器以同源 `/runtime-ws`（cookie + Origin 双重校验）连接
  宿主；宿主以服务端持有的 daemon token 用 `Authorization: Bearer` 连 daemon
  并转发 JSON-RPC 帧。**唯一例外**：指向非宿主自身项目的请求由宿主以 typed
  `not_found` 拒绝，拒绝帧不进入 daemon（边界与残余风险见
  `docs/web-console/formal-host.md` §4.1）。业务语义（negotiate/open/submit/
  cancel/steer/watch/history/artifacts/config/approval/recovery）仍全部由 daemon
  的 `AgentRuntimeService` 执行；浏览器断线不取消 daemon 中的 turn。
- **移除的硬编码/运行依赖**：前端不再硬编码 token/project_id/workspace，
  不再经 `/api/project-meta` 读 `test_token.txt`；Vite dev 只做静态热更新并把
  `/api`、`/runtime-ws` 代理到宿主（转发请求的 `Origin` 重写为宿主 origin，
  不构成认证旁路）。
- **安全边界**：仅 loopback 单用户（配置层强制），非公网多租户产品；配对码是
  真正的门，`Host`/`Origin`/`Sec-Fetch-Site` 只算纵深防御（客户端可伪造）；
  静态路径遍历/符号链接逃逸、帧/请求大小、缓存规则均有测试。验证见
  `tests/test_web_console_host.py`、`tests/test_web_console_security.py`、
  `tests/test_web_console_vertical.py` 与 `web/tests/consoleAuth.test.ts`。

## 导出会话

将对话记录导出为 Markdown：

```bash
synapse sessions export <thread_id> -f md
# 默认导出到 .coding-agent/exports/<thread_id>.md

# 加 --stdout 直接输出到终端
synapse sessions export <thread_id> -f md --stdout
```

## Codex 会话导入

支持查看和导入 OpenAI Codex 的历史会话记录。

### 扫描 Codex 会话

```bash
# 列出所有 Codex 会话
synapse sessions codex-list

# 限定工作目录
synapse sessions codex-list -w /path/to/project

# 指定 Codex 数据目录
synapse sessions codex-list --codex-home ~/.codex
```

### 预览和导入

```bash
# 查看会话元信息
synapse sessions codex-inspect <native_id>

# 预览对话内容
synapse sessions codex-preview <native_id>
synapse sessions codex-preview <native_id> -n 50 --offset 100

# 导入为 Synapse 会话
synapse sessions codex-import <native_id>
```

导入时会进行安全检查：过滤内部提示内容、校验文件完整性、跳过不支持的旧版格式。

## 全局项目目录（跨项目管理）

会话数据默认按项目（workspace）隔离在各自的 `<workspace>/.synapse/` 下。
Synapse 在用户层维护一份**全局项目目录**（`~/.synapse/catalog.sqlite`），
只读投影每个已注册项目的会话元数据与运行记录，用于跨项目查看与搜索。

### 工作原理

- 每次启动 TUI 时自动注册当前项目（`projects` 表）并投影会话（`project_sessions` 表）；
- 每轮对话结束后，会话摘要增量写入项目库并同步到目录；
- 项目库始终是数据真源，目录只是投影；`projects sync` 可随时全量对账；
- 跨项目引用使用 `(project_id, thread_id)` 复合标识，不修改现有 thread_id。

### 常用命令

```bash
# 列出所有已注册项目（按最近活跃排序）
synapse projects list

# 查看单个项目及其最近会话
synapse projects show <id|名称|路径>

# 列出某个项目的会话（含摘要）
synapse projects sessions <id|名称|路径>

# 跨项目搜索会话标题/摘要
synapse projects search jwt

# 手动对账当前项目的会话到目录
synapse projects sync

# 查看项目运行记录（TUI/CLI 启动历史）
synapse projects runs

# 目录聚合统计
synapse projects stats

# 会话列表跨项目模式
synapse sessions list --all-projects
```

### 配置

| 变量 | 默认值 | 说明 |
|---|---|---|
| `AGENT_PROJECT_CATALOG_ENABLED` | `true` | 启用全局项目目录 |
| `PROJECT_CATALOG_PATH` | `~/.synapse/catalog.sqlite` | 目录数据库路径 |
| `SESSION_SUMMARY_MODE` | `local` | `off` 关闭；`local` 每轮生成确定性本地摘要（不调用模型） |
| `SESSION_SUMMARY_MAX_CHARS` | `600` | 摘要最大字符数（超出裁剪最旧条目） |

### 会话摘要

`local` 模式下，每轮结束后把（任务、工具、进展）合并进会话的
`summary` 字段（`sessions.summary` 列），供全局列表与搜索使用。
摘要只含工具名与回答开头片段，不包含工具输出原文或密钥等敏感内容。

## TUI 多会话并发与运行状态

点击 topbar 左侧的 `≡ workspace` 打开既有项目/session 侧栏。当前项目中正在运行的
session 会排在前面，并显示 `[running] / [starting] / [queued] / [cancelling]`；不会在
主内容区额外占用一列。侧栏保持打开时，运行状态会自动刷新。

- 在侧栏选择 session 等价于 `/switch`；切换只分离/挂接渲染，不取消后台 turn；
- 后台 session 与前台 session 可并行执行，topbar 标题显示 `[N bg]` 后台运行数；
- 再切回运行中的 session 时，会重新挂接其事件流并恢复前台 busy/cancel/steer 状态。

使用要点：

- 前台会话运行中提交输入会被引导为 steer（不开启新 turn）；要并发启动另一个 loop，
  先从项目/session 侧栏切到目标 session 再提交。
- 切走再切回仍处于运行中的会话是安全的：渲染重新挂接，运行中的 turn 不会被打断，
  新模型绑定会在该 turn 结束后自动生效。

## 配置

| 变量 | 默认值 | 说明 |
|---|---|---|
| `CHECKPOINT_BACKEND` | `sqlite` | `sqlite`（持久化）或 `memory`（不保存） |
| `CHECKPOINT_PATH` | `.coding-agent/checkpoints.sqlite` | 数据库文件路径 |
| `SESSIONS_PATH` | — | 会话元数据单独存储路径 |

## 对话压缩

当对话上下文接近模型窗口限制时，Synapse 会自动触发压缩（compact）：

- 保留最近的对话 + 之前的摘要
- 默认在 ~85% 窗口时触发，保留 ~10%
- 可通过 `AGENT_ENABLE_COMPACT_TOOL=false` 关闭

## 注意事项

- `memory` 后端不持久化，重启后会话丢失
- 检查点数据库是 SQLite 格式，可以用任何 SQLite 工具查看
- 会话数据存储在项目目录的 `.coding-agent/` 下，纳入 `.gitignore` 建议忽略
