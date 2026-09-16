
Codex 用量 / 重置额度是**独立的账户级**面（三个方法，全部按 `SessionRef` 限定路由）：

- `runtime.codex.usage.get`（`codex.usage.read`，只读）：返回 `CodexUsageView`
  `{session, model, primary, secondary, captured_at, available_reset_count}`，两个窗口各有
  `used_percent` / `window_minutes` / `reset_at`，全部必有且可空。`window_minutes` 是上游真实窗口
  长度（控制台据此标注 `5h` / `7d`，不再沿用 TUI 的硬编码 `1d`）。`captured_at` 与 `reset_at`
  都是 Unix **秒**。
- `runtime.codex.reset_credits.get`（`codex.usage.read`，只读）：返回
  `CodexResetCreditsView` `{session, model, available_count, credits}`，每行
  `{id, reset_type, status, granted_at, expires_at, title, description}`（`granted_at` /
  `expires_at` 是**额度自身**的到期时间）。
- `runtime.codex.reset_credits.consume`（`codex.reset.consume`，**写**）：`expected_model` 必须
  等于会话当时的有效模型（否则 `conflict`），`confirmed` 必须是字面量 `true`（DTO 与 wire
  decoder 各校验一次），`command_id` 是客户端生成的幂等键并作为上游 `redeem_request_id`。
  返回 `CodexConsumeResult` `{session, model, command_id, outcome}`，`outcome` 是五个字面量之一
  （`reset` / `alreadyRedeemed` / `nothingToReset` / `noCredit` / `unknown`）；未知上游动词一律
  归一为 `unknown`，不原样透传上游文本。

两个读能力与写能力**互相独立**：`codex.usage.read` 不授权兑换，`session.read` 也不授权其中任何一个。
兑换是不可撤销的账户级操作（消耗该 OAuth 账户的 1 次重置额度，**不只影响当前会话**），因此
daemon 侧还有一层：`command_id` 幂等账本（同键同指纹复用结果、异键同指纹 `conflict`）、同额度
未决请求不再以新键重发、以及“兑换前重新读取额度列表并确认该额度为 `available` 且未过期”。

该面**不投影任何凭据**：没有 access token、account id、请求头、URL，也没有 OAuth 授权的
`expires_at`（那是令牌到期时间，不是额度重置时间）。上游异常与响应正文被替换为固定文案，
不进入 RPC 错误。`runtime.config.get` 因此新增一个只读字段 `codex_usage_enabled`（默认
`false`）：只有在「daemon 已注入 provider + 会话已打开 + 该会话**实际**选中的 profile
`auth == "openai_oauth"`」三者同时成立时才为 `true`；客户端不得按模型名猜测。
| `runtime.codex.reset_credits.consume` | `session`, `expected_model`, `credit_id`, `command_id`, `confirmed` | `CodexConsumeResult` |
| `runtime.codex.reset_credits.get` | `session`, optional `force` | `CodexResetCreditsView` |
| `runtime.codex.usage.get` | `session`, optional `force` | `CodexUsageView` |
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

契约冻结后 wire 表共 **47 个方法**（45 个 service 方法 + `runtime.protocol.negotiate` / `runtime.events.unwatch` 两个连接态方法）。下表按方法名排序，与 `service/contract_registry.py` 的 `WIRE_METHODS` 一一对应。

| method | params 的主要字段 | result |
|---|---|---|
| `runtime.artifacts.list` | `session`, optional `path/cursor/limit` | `ArtifactPage` |
| `runtime.artifacts.read` | `ref: {session,path}`, optional `offset/limit/expected_revision` | `ArtifactChunk` |
| `runtime.artifacts.stat` | `ref: {session,path}` | `ArtifactMetadata` |
| `runtime.attachments.begin` | `session`, `size`, `mime`, optional `display_name` | `BeginAttachmentResult` |
| `runtime.attachments.append` | `ref: {session,attachment_id}`, `expected_offset`, `data_base64` | `AppendAttachmentChunkResult` |
| `runtime.attachments.finish` | `ref`, `expected_size`, `expected_mime` | `FinishAttachmentResult` |
| `runtime.attachments.abort` | `ref` | `AbortAttachmentResult` |
| `runtime.attachments.stat` | `ref` | `AttachmentMetadata` |
| `runtime.attachments.read` | `ref`, optional `offset/limit` | `AttachmentChunk` |
| `runtime.config.get` | `session` | `RuntimeConfigView` |
| `runtime.events.read` | `session`, optional `after/limit/scan_limit/filter/max_event_bytes` | `EventPage` |
| `runtime.events.unwatch` | `subscription_id` | `{removed}` |
| `runtime.events.watch` | `session`, optional `after/queue_size/filter/max_event_bytes` | `{subscription_id,cursor}` then notifications |
| `runtime.fs.list` | optional `path/limit` | `DirectoryListing` |
| `runtime.project.list` | optional `limit/offset` | `ProjectListPage` |
| `runtime.project.register` | `workspace_path` | `ProjectListItem` |
| `runtime.project.thinking.set` | `project_id`, `level`, optional `command_id` | `SetProjectThinkingLevelResult` |
| `runtime.protocol.negotiate` | `versions`, optional `client` | `NegotiateResult` |
| `runtime.session.close` | `session`, optional `cancel_active/command_id` | `CloseSessionResult` |
| `runtime.session.create` | `project_id`, optional `thread_id/title/command_id` | `CreateSessionResult` |
| `runtime.session.delete` | `session`, optional `command_id` | `DeleteSessionResult` |
| `runtime.session.get` | `session` | `SessionView` |
| `runtime.session.goal` | `session` | `SessionGoalView` or `null` |
| `runtime.session.goal.set` | `session`, `objective`, optional `token_budget/command_id` | `SessionGoalResult` |
| `runtime.session.goal.edit` | `session`, `expected_goal_id`, `objective`, optional `command_id` | `SessionGoalResult` |
| `runtime.session.goal.clear` | `session`, `expected_goal_id`, optional `command_id` | `SessionGoalResult` |
| `runtime.session.goal.pause` | `session`, `expected_goal_id`, optional `command_id` | `SessionGoalResult` |
| `runtime.session.goal.resume` | `session`, `expected_goal_id`, optional `command_id` | `SessionGoalResult` |
| `runtime.session.history` | `session`, optional `before_turn/limit` | `SessionHistoryPage` |
| `runtime.session.list` | `project_id`, optional `limit/offset` | `SessionListPage` |
| `runtime.session.mcp.reload` | `session`, optional `server/enabled/include_tools/command_id` | `ReloadMcpResult` |
| `runtime.session.open` | `session`, optional `command_id` | `OpenSessionResult` |
| `runtime.session.rebind` | `session`, `model`, optional `command_id` | `RebindSessionResult` |
| `runtime.session.reconcile` | `session`, optional `probe_turn_ids` | `SessionRecoverabilityView` |
| `runtime.session.rename` | `session`, `title`, optional `command_id` | `RenameSessionResult` |
| `runtime.session.search` | `project_id`, optional `text/limit/offset` | `SessionSearchPage` |
| `runtime.session.thinking.set` | `session`, `level`, optional `command_id` | `SetThinkingLevelResult` |
| `runtime.turn.approval.get` | `session`, `expected_turn_id` | `PendingApprovalView` |
| `runtime.turn.approval.resume` | `session`, `expected_turn_id`, `decisions`, optional `command_id` | `ResumeTurnResult` |
| `runtime.turn.cancel` | `session`, `expected_turn_id`, optional `reason/command_id` | `CancelTurnResult` |
| `runtime.turn.steer` | `session`, `expected_turn_id`, `text`, optional `command_id` | `SteerTurnResult` |
| `runtime.turn.submit` | `session`, `text`, optional `command_id/config_overrides/attachment_refs` | `CommandReceipt` |

`filter` 是 `{kinds: string[], turn_ids: string[]}`；read 还接受 `limit`、`scan_limit`、`max_event_bytes`，watch 不接受 read-only 分页字段。artifact `data_base64` 原样传输。attachments 走独立六方法：一个 chunk 解码后不超过 256 KiB（base64 上限 `MAX_CHUNK_BASE64_CHARS`），始终落在 1 MiB frame 内；`begin` / `append` / `finish` / `abort` 由 `attachments.write` 授权，`stat` / `read` 由 `attachments.read` 授权，均按 `SessionRef` 限定。`runtime.turn.submit` 新增可选 `attachment_refs`（不透明 id 数组，最多 8 个），服务端从可信 session workspace 解析；旧 in-process `attachments` 对象仍被 wire 拒绝非空，两来源不可混用，且 `text` 与 `attachment_refs` 至少提供其一。Web 侧上传、取消与历史缩略图均已接线。

### 会话管理（additive）

- `runtime.session.list`（`session.list`，project scope，`limit` 1..100 / `offset` 0..100000）与 `runtime.session.history`（`session.read`，`limit` 1..100、`before_turn` ≥ 1）是只读面；`runtime.session.reconcile`（`session.read`）接受最多 32 个去重 `probe_turn_ids`。
- `runtime.session.create`（`session.create`，project scope）写一条会话元数据行：`thread_id` 可省略，省略时**由服务端分配**并在结果里返回，客户端不得自行编造 id；`title` 可选、非空、≤ 120 字符。它**不是** `runtime.session.open`：不打开 runtime。
- `runtime.session.rename`（`session.rename`）要求 `title` 非空且 ≤ 120 字符；会话不存在是 `not_found`，不会创建数据库文件或 schema。
- `runtime.session.delete`（`session.delete`）只删元数据行与 thread goal；结果恒带 `retained_history: true`，checkpoint 与 transcript projection 保留，因此 UI **不得**声称对话被擦除。正在运行回合的会话原子地以 `conflict` 拒绝，且不取消该回合。
- `runtime.session.search`（`session.search`，project scope）是**元数据搜索**（title / summary / thread_id / model / active_model），`text` ≤ 200 字符、`limit` 1..100、`offset` 0..100000；它**不是** transcript 全文检索，也不会创建数据库或构建 agent。

### goal 管理写（additive）

`runtime.session.goal`（读）之外的五个写方法 `runtime.session.goal.set` / `.edit` / `.clear` / `.pause` / `.resume` 共用一个独立 capability `session.goal`：`session.read` 只授权读，绝不授权任一写操作。`edit` / `clear` / `pause` / `resume` 都要求 `expected_goal_id`（乐观并发，目标已被替换时返回 `conflict`）；`set` 的 `objective` 必填、`token_budget` 可选且必须为正整数，且**拒绝覆盖未完成的 goal**（`conflict`）。`pause` 只请求取消**本会话**自己的 live turn 并通过 `cancellation_requested` 报告；`resume` 只做状态转移，不会自动续跑一轮。写入落在该会话自身的 ledger 上，不用全局 `get_goal_service()` 单例，因此一个项目不会写到另一个项目的 goal。

### 项目列举与登记、宿主目录浏览（additive）

`runtime.project.list`（`project.list`，**catalog scope**，无 per-request 项目位置）只读枚举已登记项目：请求 `limit` 1..100、`offset` 0..100000，`visible_project_ids` 是服务端计算字段，wire decoder 显式拒绝客户端传入；服务端在 provider 内**先按可见性过滤再分页**。该方法不打开会话、不构建 agent、也不注册新项目。项目**登记**（`ProjectCatalog.register_project`）现在有 wire 方法：`runtime.project.register`。

`runtime.project.register`（`project.register`，**catalog scope**，无 per-request 项目位置）是**写**面：请求只有 `workspace_path`（≤ 4096 UTF-8 bytes，wire decoder 只校验长度与 NUL），daemon 解析该宿主路径、确认它是已存在目录，再 upsert 用户层项目 catalog（`~/.synapse/catalog.sqlite`）。登记按 workspace 路径**幂等**：重复登记复用同一个稳定 `project_id`，结果是与列举面相同的 `ProjectListItem` 投影。

`runtime.fs.list`（`fs.list`，**catalog scope**，无 per-request 项目位置）是**只读且有界**的宿主目录浏览：请求 `path`（`null` 表示 daemon 的 home 目录）与 `limit`（默认 200，1..1000），结果是 `DirectoryListing`（`path` / `parent`，文件系统根处 `parent` 为 `null` / `entries: DirectoryEntry[]` / `truncated` / `roots`，其中 `roots` 是平台顶层入口：Windows 为盘符、POSIX 为 `/`），`DirectoryEntry` 只有 `{name, path}`。它只枚举该目录的**直接子目录**——从不返回文件、从不递归。

两者的授权都只要求一个**项目级**（project-wide）能力授予（`project.register` / `fs.list`，与 `project.list` 一样经 `visible_project_ids` 计算），不依赖任何 per-request 项目位置。

`runtime.session.rebind` 与 `runtime.session.thinking.set` 都是**会话级写**：只替换该
thread 后续 turn 使用的 agent/settings 绑定并持久化到该会话的 model binding，绝不修改
项目默认值。`thinking.set` 的 `level` 必须落在该会话 `runtime.config.get` 公布的
`thinking_levels` 白名单内（否则 `invalid_request`），返回 `level` 为规范化后的实际等级
（关闭思考时为 `off`）与刷新后的 `RuntimeConfigView`。授权上二者各自需要独立写能力位
（`session.rebind` / `session.thinking`），`session.read` 不足以授权任一写操作。

`runtime.project.thinking.set` 是**项目级写**（参数是 `project_id`，不是 `session`）：把
`level` 写入该项目的设置层（`<workspace>/.synapse/settings.json` 的 `enable_thinking` /
`reasoning_effort`），语义是「此后**新建**会话的默认推理等级」——已打开的会话不被重绑，daemon
重启后仍生效。`level` 必须落在与 `runtime.config.get` 同一来源的白名单内（否则
`invalid_request`），授权需要独立能力位 `project.thinking`，且只接受项目级授权
（`thread_ids=None`）；`session.read` / `session.thinking` / `session.rebind` 都不授权它。返回
`{command_id, project_id, level}`（`level` 为规范化后的实际等级），**不返回设置文件路径**。
`runtime.config.get` 因此新增两个只读字段：`project_thinking_level`（项目自身默认，可能与
`thinking_level` 会话值不同）与 `can_set_project_thinking`（该 peer 是否提供项目级写端口）。

S9 增加 transport 控制方法 `runtime.protocol.negotiate`，严格参数为
`{versions: string[], client?: {name: string, version: string}}`。当前仍只有
`SUPPORTED_WIRE_VERSIONS=("1",)`；S7 客户端不协商时，首个业务请求隐式锁定 v1，保持
向后兼容。协商不属于 service dispatch，元数据仍使用连接选定的 v1。

### 尚未 wire（本表之外）

下列后端能力存在但**没有** wire 方法，本表（及整个契约）不覆盖：会话清理 `prune_empty`、会话导出（JSON / Markdown）、对话全文搜索、上下文压缩与上下文状态（TUI `/compact`、`/context`）、safety / 权限策略读写、工具输出压缩设置（TUI `/compression`）、远程身份 / 多用户 principal 管理。项目登记/更新已不再是缺口：`runtime.project.register` 覆盖 `ProjectCatalog.register_project` 的登记/更新（`touch_project` 没有单独方法），宿主目录浏览由 `runtime.fs.list` 提供。TUI 的 `/theme` 与 slash 命令解析/补全属 UI-only，不需要 RPC。逐项真源与状态见 [progress.md](progress.md) 的「尚未 wire 的后端能力（真实矩阵）」。

## Limits and lifecycle

单 text frame 默认最多 1 MiB，binary frame 拒绝（1003）；超限 frame 由 WebSocket 层关闭 1009；JSON 最大 nesting 64，单连接最多 32 inflight/32 subscriptions。输出 canonical UTF-8、compact、sorted，单消息最多 8 MiB。单 connection 一个 bounded writer queue，默认 128，范围 1..4096；满时关闭 1013，固定 reason；writer 发送失败关闭 1011，使用固定 transport reason。watch response 在 stream enter 成功后等待 writer acknowledgement，实际发送后才启动 pump；replay gap 或 enter error 只返回 request error 且不残留 subscription。断开连接只 detach，不调用 close session/cancel turn。S8 daemon ownership、S9 client 重连/版本协商见 [S9 compatibility matrix](s9-compatibility-matrix.md) 与 [ADR-S-017](adr-s-017-reconnect-version-negotiation.md)。
