# Agent Runtime Service 实施进度

> 本文件是实施状态的单一真源。阶段文档定义范围和方案，不维护实时状态。
> 状态值：`not_started`、`in_progress`、`blocked`、`completed`。
> 当前总体状态：`completed`。

## 当前工作

- S2 状态：已冻结；全部专项与回归门禁已完成。
- 当前阶段：S10（消费者迁移，`completed`）。
- 当前任务：S10 implementation 与 final gates 已完成；总体 S0-S10 `completed`。
- 下一阶段：无。
- 当前阻塞：无。

## S3 当前实现

- `EventFilter`、raw matching、`scan_limit`、原始 cursor 元数据、watch cursor 与 canonical JSON 字节上限已实现。
- 专项 `tests/test_runtime_service_events_s3.py`：20 passed；覆盖 filter/reconnect、raw cursor、精确分页、canonical UTF-8 bytes、projection/overflow 终态与 first-terminal-wins。
- 最终门禁：专项 20 passed；本轮 S1/S2 service、lifecycle、session、manager/runtime 核心回归 255 passed；全量 1955 passed, 1 skipped；Ruff、git diff --check、mkdocs strict 均通过。

## S4 安全审阅修复与门禁结果

- policy alias：stat/read 对请求逻辑 path 和 resolved target 都执行 deny、`.git`、`.gitignore`、gitignore 与默认 ignore 检查；list 对 resolved child target 命中 policy 的 alias 跳过；workspace 外 target 仍拒绝。
- 输入边界：`ArtifactRef.path='.'` 仅 list root 合法；expected revision 与 cursor 均有 UTF-8 字节上限；malformed/non-ASCII cursor 稳定返回 `invalid_artifact_cursor`。
- 竞态与异步：read 仍只向 file wrapper 请求 `limit + 1`；同步 filesystem 操作经 `asyncio.to_thread`；open/fstat revision 变化、删除或替换均不返回 chunk，并使用脱敏错误。
- 专项：`tests/test_runtime_service_artifacts_s4.py` 32 passed。
- service 回归：S1-S3 service contracts/local/events/lifecycle 122 passed。
- 静态与文档：Ruff（service 与专项测试）通过；`git diff --check` 通过；`mkdocs build --strict` 通过（仓库既有未纳入 nav 页面及 tutorial 锚点 INFO 未归因于本次变更）。

## S1 第四轮硬化修复（已完成）

第四轮针对三个已复现缺陷 + 一个语义缺口：

- **broker 有序投递（缺陷 A）**：`SessionEventBroker.emit()` 不再在线性化锁外直接调用 callback。`emit` 在锁内分配 sequence、保留 event、快照 subscriber、按 sequence 入队 broker 自有 `_delivery` 队列；仅一个 drainer（`_dispatching` 互斥）在锁外串行调用 callbacks，无常驻线程。callback 可重入 `emit`/`close`/`subscription.close`/`subscribe`/`read` 而不死锁；`close` 线性化时拒绝新 emit 并快照 `on_close` 通知集合，drainer 先投递全部 close 前已接受事件、再在锁外对每个 `on_close` 恰一次通知；unsubscribe 不抹掉已线性化 emit 的 snapshot 事件，也不抹掉已线性化的 pending `on_close`。慢 subscriber 只拖慢自身流与 close 通知时序，不阻塞 broker 锁、不重排其他 subscriber（`_dispatching` 空队列释放与入队在同一锁临界区，无丢唤醒）。
- **producer 异常脱敏（缺陷 B）**：`project_payload` 公共边界捕获普通 `Exception`（不捕获 `BaseException`，`KeyboardInterrupt`/`SystemExit`/`asyncio.CancelledError` 原样穿透），`raise ... from None` 转为 `InvalidEventPayloadError`，消息只含安全顶层类型信息，不含原异常文本/repr/payload value；内部已知 `InvalidEventPayloadError` 原样保留，depth/cycle 语义不变。覆盖 Mapping.items、dataclass getter、list/tuple 迭代、set 迭代/排序、Enum.value、bytes conversion、PathLike/`os.fspath`、datetime/Decimal/UUID 等入口。
- **first-terminal-wins（缺陷 C）**：`LocalEventStream.open()` replay 投影与 `_drain()` detached 投影 commit 统一走锁内 `_fail_locked(error)`：overflow（`_ingest`）与 projection error 谁先线性化谁胜出，后到者只清缓冲不覆盖 `_error`；一次 error 后 EOF、无 tail，overflow 优先未消费 replay。
- **failed lease 终止语义**：`LocalEventWatch.__aenter__` 失败（invalid cursor、stale gap、closed source、projection error）后 lease 标记 `_failed`，`closed` 为 True、不可再次 enter、前后 broker 无 subscriber；正常 context exit 保持幂等。

## S1 第六轮硬化修复（已完成）

第六轮针对已复现的 ordered dispatcher BaseException 状态缺陷：

- **broker 有序投递对 observer BaseException 的确定性恢复（缺陷 D）**：旧 `_dispatch()` 逐 callback 只捕获 `Exception`；callback 抛 `KeyboardInterrupt`/`SystemExit`/`asyncio.CancelledError` 时 `_dispatching` 永久为 True，后续 emit/close 不再启动 drainer，pending events 与 `on_close` 静默丢失。新语义（ADR-S-009）：普通 `Exception` 维持 observer failure isolation（继续投递、不传播）；非进程级 `BaseException`（含 `CancelledError`）不停止 drainer——同一 envelope 的后续 subscriber records 与后续 queued delivery 仍按 sequence 严格投递，记录第一 `BaseException`，drainer 完成全部已接受 delivery（及 pending `on_close`）后重新抛给认领 drainer 的 emitter/close 调用栈；`KeyboardInterrupt`/`SystemExit` 终止当前 delivery（尚未调用的 records 放弃）并立即重抛，`_dispatching` 恢复为 False、剩余 queue 保留给后续 emit/close 认领；若 broker 已关闭时进程级退出逃逸，drainer 先完成剩余 queue 与 pending `on_close` 再重抛（close work 绝不搁浅）。`_notify_closed` 对 `on_close` 采用同一策略。外部 callback/`on_close` 仍在 lock 外执行、无常驻线程、reentrant emit 只入队不递归。

## 阶段总览

| 阶段 | 状态 | 门禁结果 | 方案 |
|---|---|---|---|
| S0 基线确认 | completed | 通过 | [index.md](index.md) |
| S1 进程内纵向切片 | completed | 通过（含六轮硬化用例） | [index.md](index.md) |
| S2 会话生命周期命令扩展 | completed | 专项 25、核心 143、回归 67、全量 1935 passed；Ruff/diff/mkdocs strict 通过 | [index.md](index.md) |
| S3 事件契约收口 | completed | 专项 20、核心 255 passed；全量 1955 passed、1 skipped；Ruff/diff/mkdocs strict 通过 | [index.md](index.md) |
| S4 Artifact 端口 | completed | 专项 32 passed；S1-S3 service 回归 122 passed；Ruff 与 diff check 通过；MkDocs strict 通过 | [index.md](index.md) |
| S5 多 manager 路由硬化 | completed | 专项 24、service/catalog 回归 168、manager/session 回归 66；Ruff、diff check、MkDocs strict 通过 | [index.md](index.md) |
| S6 鉴权与权限边界 | completed | 专项 69、核心 388、全量 2081 passed、1 skipped；Ruff、git diff --check、MkDocs strict 通过 | [index.md](index.md) |
| S7 网络传输 | completed | 专项总计 60（protocol 28、WebSocket 32）passed；transport lifecycle hardening、service/runtime/ACP 回归、Ruff、diff、MkDocs strict 与全量门禁通过 | [index.md](index.md) |
| S8 daemon 进程 | completed | 第二轮专项 22 passed、1 skipped；安全扫描、S7、S1-S6 核心、Ruff、diff、MkDocs strict、uv build 与全量 pytest 证据见下文 | [index.md](index.md) |
| S9 重连与版本协商 | completed | S9 专项 94 passed；S9+S7 transport 154 passed、9 warnings；S8 22 passed、1 skipped；核心 467 passed；全量 2257 passed、2 skipped、9 warnings；Ruff、diff check、MkDocs strict、uv build 通过 | [index.md](index.md) |
| S10 消费者迁移 | completed | implementation、final gates 与 review 完成 | [index.md](index.md) |

## S8 daemon 进程与生命周期

- 新增 `synapse.runtime.daemon`：配置、受限 token 文件、精确 Bearer 认证、daemon 专用 ACL、跨平台 held lock、owner-matched atomic metadata、foreground `RuntimeDaemon` 与 console/module entry。
- composition root 严格复用 S7 transport、S6 access wrapper、S5 router 与既有 RuntimeManager/SessionRuntime 执行栈；manager 按精确 catalog project id 加载 project settings，并使用 session-scoped coding-agent factory。
- S8 专项三文件 `tests/test_runtime_daemon_s8.py`、`tests/test_runtime_daemon_s8_hardening.py`、`tests/test_runtime_daemon_s8_subprocess.py`：22 passed、1 skipped；最后一个文件仅保留安全说明，未启动真实 daemon。覆盖 one-shot lifecycle、startup rollback injection、fixed CLI errors、mock signal rollback、private state directory、bounded metadata/token files 与 shared shutdown error identity。
- 本轮安全扫描确认 S8 测试不含 subprocess/Popen/create_subprocess/os.kill/process terminate/kill 或 signal.raise_signal；signal 测试直接调用 mock handler callback。
- 按用户安全约束，本轮未执行真实子进程 daemon 测试、真实 signal 发送或任何需要外部终止的常驻进程；文档仅记录这一限制，不将其表述为真实子进程验证证据。

### S8 第二轮实际门禁

```text
uv run --no-sync pytest tests/test_runtime_daemon_s8.py tests/test_runtime_daemon_s8_hardening.py tests/test_runtime_daemon_s8_subprocess.py -q
22 passed, 1 skipped in 5.43s

uv run --no-sync pytest tests/test_runtime_transport_s7.py tests/test_runtime_transport_websocket_s7.py -q
60 passed, 8 warnings in 53.32s

uv run --no-sync pytest tests/test_runtime_service_access_s6.py tests/test_runtime_service_artifacts_s4.py tests/test_runtime_service_contracts.py tests/test_runtime_service_events_s3.py tests/test_runtime_service_lifecycle.py tests/test_runtime_service_local.py tests/test_runtime_service_routing_s5.py tests/test_runtime_manager.py tests/test_session_runtime.py tests/test_runtime_streaming.py tests/test_runtime_hardening.py tests/test_agent_turn_runtime.py tests/test_project_runtime.py tests/test_acp_p0_baseline.py tests/test_acp_p1_core.py tests/test_acp_p1_transport.py tests/test_acp_p2_content.py tests/test_acp_p2_events.py tests/test_acp_p2_updates.py tests/test_acp_p3_agent.py tests/test_acp_p3_permissions.py tests/test_acp_p4_history.py tests/test_acp_p4_lifecycle.py tests/test_acp_p5_mcp.py tests/test_acp_p6_client_services.py tests/test_acp_p8_compliance.py -q
467 passed in 44.15s

uv run --no-sync ruff check .
All checks passed

git diff --check
passed

uv run --no-sync mkdocs build --strict
exit 0; only pre-existing unlisted-page/link INFO and Material warning

uv build
built dist/synapse_cli_agent-0.1.43.tar.gz and dist/synapse_cli_agent-0.1.43-py3-none-any.whl

uv run --no-sync pytest -q
2257 passed, 2 skipped, 9 warnings
```

以上命令均未启动需要外部终止的 daemon。S8 测试门禁前已扫描 `subprocess`、`Popen`、`create_subprocess`、`os.kill`、`process.terminate`、`process.kill`、`signal.raise_signal`，结果为 `clean`。

## S9 重连、版本协商与兼容矩阵（已完成）

- S9 专项：94 passed。
- S9 + S7 transport：154 passed、9 warnings。
- S8：22 passed、1 skipped；核心回归：467 passed。
- 全量：2257 passed、2 skipped、9 warnings。
- Ruff、`git diff --check`、`mkdocs build --strict` 与 `uv build` 均通过。
- 本轮仅记录主流程独立权威门禁结果；未声称执行真实子进程验证，不推进 S10。

## S7 JSON-RPC/WebSocket 传输（已完成）

- 新增 `src/synapse/runtime/transport/protocol.py`：strict JSON-RPC 2.0 parsing/encoding、duplicate-key and non-finite-number rejection、wire DTO conversion、fixed error mapping and canonical output。
- 新增 `src/synapse/runtime/transport/websocket.py` 与公共导出：websockets 15 asyncio server、headers-to-Principal authentication、per-connection service factory、bounded writer、request/subscription limits、response-first watch and detach cleanup。
- 新增 ADR-S-015 与独立 wire table；未增加依赖、未新增 daemon/console script，attachments wire 仍不支持非空数组，CLI/TUI/ACP 未迁移。
- S7 门禁已完成：两个 S7 文件专项总计 60 passed（protocol 28、WebSocket 32）；subscription terminal error、active-turn detach、严格 reservation/inflight、overflow/writer failure、并发 cleanup、ack barrier、server close ownership 与 AST/import/constructor guard 均覆盖。S1-S6 service 回归、runtime manager/session/turn、ACP baseline、全仓 Ruff、diff、MkDocs strict 与全量 pytest 均已执行并通过。

## S6 进程内 ACL

- `Principal`、`AclGrant`、`AccessRequest` 为 frozen/slotted 数据对象；grant
  在构造时完成 copy isolation、scope 规范化与 capability 白名单校验。
- `AclAuthorizer` 使用 immutable rule snapshot，按 subject/project/thread/capability
  精确匹配；默认拒绝，拒绝只返回固定 `permission_denied`。
- `AccessControlledAgentRuntimeService` 覆盖全部 11 个应用端口，先做最小
  `invalid_request` shape 校验，再授权，再调用 delegate；unknown resource 不
  触达 delegate，避免存在性 oracle。
- watch 在创建 lease 时授权并采用 capability snapshot；lease detach/exit 只
  关闭 subscription，不取消 turn。S6 不读取 runtime safety/settings，也不迁移
  CLI/TUI/ACP。ADR 见 [ADR-S-014](adr-s-014-access-control.md)。

### S6 门禁结果（实际执行）

- `tests/test_runtime_service_access_s6.py`：69 passed；实际覆盖全部 11 个端口的
  capability separation、非 oracle 拒绝、精确 scope、malformed DTO、真实
  Local service artifact/lifecycle、watch replay/live detach、delegate 异常与
  constructor/import guard。
- 本轮生产修复：拒绝空 `AclGrant`；wrapper 对 exact principal/authorizer 类型和
  delegate 所需方法做 fail-fast 结构校验；malformed access DTO 不触达授权器或
  delegate；`PermissionDeniedError` 的 code 覆盖仍固定为 `permission_denied`。
- S1-S5 service 回归、RuntimeManager/SessionRuntime、ACP permission 与安全专项、
  Ruff、`git diff --check`、全量测试、`mkdocs build --strict` 的真实结果见本轮
  结束记录；未提交 git、未推进 S7。
- 本轮门禁实绩：核心汇总 388 passed；全量 2081 passed、1 skipped；Ruff、diff
  check 与 MkDocs strict 均通过（MkDocs 仅报告仓库既有未纳入 nav/锚点 INFO）。
- 本轮未提交 git、未推进 S7，也未迁移 CLI/TUI/ACP。

## S1 任务

- [x] S1-01 新建 `src/synapse/runtime/service/` 包：`commands.py`、`queries.py`、`events.py`、`errors.py`、`ports.py`、`local.py`。
- [x] S1-02 定义最小公共 DTO：`SubmitTurnCommand`、`CommandReceipt`、`GetSessionQuery`、`SessionView`、`ReadEventsQuery`、`EventCursor`、`RuntimeEvent`、`EventPage`。
- [x] S1-03 DTO 全部 frozen；`config_overrides` 构造时 `deepcopy` 隔离 + 顶层只读；事件 payload 用递归 JSON normalizer 严格投影（`json.dumps(..., allow_nan=False)`），不暴露 `TurnEvent` 实例。
- [x] S1-04 `ports.py` 定义传输无关 Protocol（`EventWatch`/`EventStream` 双 Protocol），不导入 UI/CLI/ACP/传输/框架类型。
- [x] S1-05 实现 `LocalAgentRuntimeService`：`submit_turn` / `get_session` / `read_events` / `watch_events`，注入 `manager_provider` 按 project_id 路由。
- [x] S1-06 submit 只调用 `RuntimeManager.submit_ref`；receipt 只在 manager 取得并发配额并启动 turn 后返回（真实背压，无第二个 command queue）。
- [x] S1-07 错误语义：sessions 层 typed 异常（`SessionBusyError`/`RuntimeClosedError`/`InvalidEventCursorError`）+ service 错误码 `not_found`/`conflict`/`replay_gap`/`closed`/`invalid_session`/`event_overflow`/`invalid_cursor`/`invalid_request`/`invalid_event_payload`；普通 `RuntimeError` 原样上抛。
- [x] S1-08 `SessionEventBroker` 新增 `SessionEventWindow`、`read_after`、`subscribe_from`（严格游标 `0..latest`，stale 判 gap 且不注册）；`_dropped_through` 累计驱逐判定 gap；`_LOSSLESS` 保留新增硬上限。
- [x] S1-09 `watch_events` 返回 context-only lease（未 enter 不注册；订阅延迟到 `__aenter__`）；broker 回调经 `threading.Lock` 有界 ingress + 单 drain 合并投递；溢出吸收性终态（恰一次 `event_overflow` 后 EOF，无 tail）；source close 唤醒 blocked stream 且先消费已接受事件再 EOF；关闭 watcher 不关闭/取消 session。
- [x] S1-10 新增测试 `tests/test_runtime_service_contracts.py`、`tests/test_runtime_service_local.py`，并在 `tests/test_session_runtime.py` 补 broker 用例。
- [x] S1-11 第三轮硬化修复（全部经 Event/barrier 确定性测试验证）：
  - `_drain()` 锁内仅 detach 批次，锁外执行 JSON 投影，提交时重新检查 overflow/终止状态——慢/自定义 Mapping payload 投影不再阻塞 runtime producer 的 `_ingest()`；投影期间并发 overflow 丢弃批次、不写已终止 stream，`_pending` 不变量保持。
  - `_schedule_drain()` 在 event loop 已 closed 的 `RuntimeError` 路径锁内抓取 subscription、锁外 `close()`，broker registry 不泄漏。
  - `SessionEventBroker.close()` 在 close 线性化时快照待通知 records（`_pending_close`）；并发 `subscription.close()` 不能删除 pending `on_close`；accepted event 先于 `on_close`；恰一次、无死锁（含重复 close、close callback 重入、多 inflight emit）。
  - `open()` 的 replay 投影在锁外进行，发布 replay 时在 `_ingress_lock` 内重新检查 overflow/closed/error：已终止不写回；source close 仍允许已捕获 replay 发布后 drain 到 EOF。
  - JSON 错误消息脱敏：非字符串 mapping key 只报告类型（如 `non-string mapping key of type 'bytes'`），non-finite float 不回显具体值，未知类型可报告 type。
  - set 投影排序改用规范 JSON key（`json.dumps(sort_keys=True, allow_nan=False, separators=(",",":"), ensure_ascii=False)`），不再使用 repr fallback；dict 插入顺序不影响 set 投影顺序。
  - `read_after`/`subscribe_from` 对 `bool`/`float`/`str` 等非真正 int 游标抛 `InvalidEventCursorError`（`False` 不再当 `0`）；异常保存原始 `requested`（类型标注为 `object`），消息只报告类型与范围，service 映射 `InvalidCursorError`。
- [x] S1-12 第四轮硬化修复（新增 24 个确定性用例，含 barrier/Event 竞态测试）：
  - broker 有序投递：`emit` 锁内入队 + 单一 `_dispatch` drainer 锁外串行 callback（`_dispatching` 互斥，无常驻线程），subscriber 严格按 sequence 顺序；callback 可重入 broker API；`close` 快照 `on_close` 集合，drainer 先投递 close 前已接受事件再恰一次通知；unsubscribe 语义固定（已线性化 snapshot 事件仍投递、pending on_close 不可抹除）。慢 subscriber 只拖慢自身与 close 通知，不阻塞锁、不重排其他 subscriber。
  - `project_payload` 公共边界把 producer 代码（Mapping.items / dataclass getter / 迭代 / Enum.value / bytes conversion / `os.fspath` / isoformat / Decimal / UUID 等）抛出的普通 `Exception` 归一化为 `InvalidEventPayloadError`（`raise ... from None`，消息只含安全顶层类型，`__cause__ is None`）；不捕获 `BaseException`；内部已知错误原样保留。
  - first-terminal-wins：`open()` replay 投影与 `_drain()` detached commit 统一 `_fail_locked`，overflow 与 projection error 先线性化者胜出；恰一次 error 后 EOF、无 tail。
  - `LocalEventWatch.__aenter__` 失败后 lease 永久 closed/failed 且不可再 enter，broker 前后无 subscriber；正常 exit 幂等。
- [x] S1-13 第五轮硬化修复（新增 7 个确定性用例）：
  - `project_payload` 公共边界改用私有可信标记 `_ProjectionRejected`：`_project` 内所有服务自身 rejection（depth/cycle/key/NaN/unknown）改抛该标记，边界只认该标记并转为精确公开 `InvalidEventPayloadError`（安全消息保留、`raise ... from None`）；producer 自抛的 `InvalidEventPayloadError` 不再被 `except InvalidEventPayloadError: raise` 放行——它与普通 `Exception` 一样统一新建脱敏错误（消息只含安全顶层类型，`__cause__ is None`），任意嵌套深度都经过公共边界；`BaseException` 仍原样穿透（不捕获）。
  - `LocalEventStream.open()` replay 投影与 `_drain()` live 投影对 `BaseException`（`KeyboardInterrupt`/`SystemExit`/`asyncio.CancelledError`）确定性清理：任何异常路径都关闭已注册 subscription（broker registry 不泄漏）、failed lease 可观察且不可重入；live 侧进入终态 EOF 并唤醒 reader，原异常原样传播（不转 service error、不吞、不永久挂起）。
  - `SessionEventBroker.forward_to()` 改为与 live 共用有序 dispatcher：订阅注册与全部 replay 入队在同一锁临界区，并发 `emit()` 严格排在其后，replay 全部投递后才投递 live（barrier 测试确定性验证）；callback 锁外、可重入、callback 内 close 不死锁且 accepted replay 先投递；`subscribe()` 的“调用者手动消费 replay”兼容语义不变。
- [x] S1-14 第六轮硬化修复（新增 8 个确定性用例）：
  - `SessionEventBroker._dispatch()` 对 observer `BaseException` 的确定性恢复（ADR-S-009）：非进程级 `BaseException` 继续投递同一 envelope 后续 records 与后续 queued delivery、记录第一者并在 drain 完成后重抛给认领 emitter；`KeyboardInterrupt`/`SystemExit` 终止当前 delivery、恢复 `_dispatching`、保留剩余 queue 供后续 drainer 接管；broker 已关闭时的进程级退出先完成剩余 queue 与 pending `on_close` 再重抛。`_notify_closed` 采用同一策略。普通 `Exception` 隔离、多次 close 无双通知、`_dispatching` 最终 False、delivery 为空、close notification 恰一次均有测试固定。

> 有界性说明：当前 S1 的队列/回放有界性按**事件数**与**投影深度**（`_MAX_PROJECTION_DEPTH`）界定；单事件 payload 的**字节级上限不在 S1 承诺范围内**（有序投递队列同样只按事件数有界，不承诺单事件字节有界），留给 S3/传输层收口。

## S1 第五轮风险说明

- `forward_to` 的 replay 投递现在经由串行 drainer 执行：若 sink callback 阻塞，调用 `forward_to` 的线程会同步等待该 callback 返回（与旧实现同步投递 replay 等价），但不会阻塞 broker 锁、不阻塞其他 subscriber；病态 sink 只拖慢自身流。
- live `_drain` 遇到 `BaseException` 时在清理后原样重抛：对 `KeyboardInterrupt`/`SystemExit`，事件循环按 asyncio 语义终止（进程本就在被中断）；reader 侧观察到的是确定性的终态 EOF，不是 service error。
- `_ProjectionRejected` 是模块私有信任标记（未导出）；仅当 producer 代码刻意构造该精确类时才可能绕过脱敏，属可接受信任边界。
- `forward_to` 的 sink callback 异常现在与 live 投递一致地被 drainer 隔离（吞掉），不再从 `forward_to` 调用点传播；当前无生产调用方依赖旧的传播行为（TUI 走 `SessionRuntime.subscribe` 手动 replay 路径，不受影响）。

## S1 第六轮风险说明

- 非进程级 `BaseException`（含 `asyncio.CancelledError`）会原样重抛给认领 drainer 的 emitter/close 调用栈：调用方必须自行决定是否终止；broker 自身状态在重抛前已一致（`_dispatching` 已恢复、queue 为空或保留、close notification 已标记），后续 emit/close 可继续接管。不会把进程级 `KeyboardInterrupt`/`SystemExit` 安全转换为业务错误。
- `KeyboardInterrupt`/`SystemExit` 终止当前 delivery 时，该 envelope 尚未调用的 subscriber records 会被放弃（ADR-S-009 明确定义），但剩余 queued delivery 保留在 `_delivery` 中，可由后续 emit/close 认领的 drainer 投递；若此后没有任何 emit/close，队列会保留在 broker 中（对进程级退出场景属可接受语义，close 通知在 broker 已关闭时由当前 drainer 保证完成）。
- 同一次 drain run 中后续的 `BaseException` 会被第一者取代（只重抛第一者），属文档化语义；普通 `Exception` 仍逐 callback 隔离，不受影响。

## S1 门禁结果

- [x] 门禁 1：DTO frozen / 嵌套 override 防外部突变 / 严格 JSON-safe；import guard 无 Textual/Typer/ACP/Rich/HTTP/WebSocket/LangChain。
- [x] 门禁 2：submit 仅调用 `manager.submit_ref`；receipt 背压语义（取得并发配额并启动 turn 后才返回）；running/settled `SessionView`。
- [x] 门禁 3：typed busy/closed 映射 `conflict`/`closed`；普通 `RuntimeError`（文本含 `closed`/`active turn`）不吞不误分类。
- [x] 门禁 4：两 session 事件隔离；read 用 session sequence 且保留 turn-local sequence。
- [x] 门禁 5：replay+live 无 gap/无重复；watch close 后 turn 继续并 settle。
- [x] 门禁 6：严格游标（read/watch 拒绝 negative/future）；stale cursor 明确 `replay_gap` 且不注册；空 broker / cursor=latest 行为正确。
- [x] 门禁 7：有界队列溢出明确 `event_overflow`（恰一次后 EOF，优先于未消费 replay）且订阅清理；burst 下 `call_soon_threadsafe` 合并为常数次、producer 不阻塞、逻辑 pending 有界。
- [x] 门禁 8：架构护栏——service 不实例化 `AgentTurnRuntime`、不调用 `stream_agent`/`agent.ainvoke`；lease 不可被裸 `async for` 迭代。
- [x] 门禁 9：producer 自抛 `InvalidEventPayloadError` 与普通 `Exception` 一样经公共边界脱敏（新错误、`from None`、无 secret/cause）；内部可信 rejection 以精确公开类型保留安全消息；`BaseException` 原样穿透。
- [x] 门禁 10：replay/live 投影中的 `BaseException` 确定性清理（订阅关闭、failed lease 可观察且不可重入、live 终态 EOF 唤醒 reader）；不转 service error、不吞 `KeyboardInterrupt`/`SystemExit`/`CancelledError`。
- [x] 门禁 11：`forward_to` replay 与 live 共用有序 dispatcher（并发 emit 不越过 replay、严格 sequence 顺序）；callback 锁外可重入；callback 内 close 不死锁且 accepted replay 先投递。
- [x] 门禁 12：ordered dispatcher 对 observer `BaseException` 的确定性恢复——非进程级 `BaseException` 不永久 `_dispatching=True`、不丢已接受 delivery/close notification（第一者重抛给认领 emitter，同 envelope 后续 subscriber 继续投递）；`KeyboardInterrupt`/`SystemExit` 终止当前 delivery 且剩余 queue 可由后续 drainer 接管；broker 已关闭时 close work 恰一次完成；普通 `Exception` 隔离与多 subscriber 顺序仍通过。

## S1 硬化验证结果（真实执行）

第六轮修复后全量重跑（新增 8 个确定性用例；数值为本轮实际运行输出）：

```text
uv run --no-sync pytest tests/test_runtime_service_contracts.py tests/test_runtime_service_local.py tests/test_session_runtime.py -q
129 passed in 1.61s

uv run --no-sync pytest tests/test_runtime_manager.py tests/test_agent_turn_runtime.py tests/test_runtime_streaming.py tests/test_runtime_hardening.py tests/test_project_runtime.py -q
81 passed in 7.36s

uv run --no-sync pytest -q
1910 passed, 1 skipped in 178.43s (0:02:58)

uv run --no-sync ruff check src/synapse/runtime/service/events.py src/synapse/runtime/service/local.py src/synapse/runtime/sessions/events.py tests/test_runtime_service_contracts.py tests/test_runtime_service_local.py tests/test_session_runtime.py
All checks passed

uv run --no-sync mkdocs build --strict
构建成功（exit 0）；agent-runtime-service/ 无告警，仓库既有 tutorial.md 锚点 INFO 另计
```

## 验证命令

## S10 最终门禁（已完成）

- S10 implementation 与 final gates 已完成；总体 S0-S10 状态为 `completed`。
- TUI C1/C2 最终明确清单：443 passed。
- 安全全仓最终结果：2348 passed、2 skipped。以下 13 个文件明确排除，因其涉及 process API 安全约束或 socket 环境不稳定；不将排除项声称为通过：
  `test_acp_p0_baseline.py`、`test_acp_p1_transport.py`、`test_agent_turn_runtime.py`、`test_backends.py`、`test_git_chrome.py`、`test_herdr_integration.py`、`test_runtime_service_routing_s5.py`、`test_runtime_transport_s7.py`、`test_startup_trace.py`、`test_transcript_migration.py`、`test_runtime_daemon_s8.py`、`test_runtime_transport_client_methods_s9.py`、`test_runtime_transport_websocket_s7.py`。
- 其他最终证据：lifecycle/consumer 核心 194 passed；permit2 5、crossloop 5、generation 4；真实 approval 9；CLI registry 3；project exact/generation tests 通过。
- Ruff、`git diff --check`、`uv run --no-sync mkdocs build --strict`、`uv build` 均通过；构建包版本为 `0.1.43`。
- 三轮 review 最终无阻塞发现；workspace freeze 的 Medium 问题已修复。
- 执行链保持唯一：`AgentRuntimeService -> RuntimeManager.submit_ref/resume_ref -> SessionRuntime -> AgentTurnRuntime`。consumer 不变量保持：DTO ports 不暴露 runtime 执行对象；watch detach/connection close 不 cancel turn；consumer close 由同 loop owner 负责并使用 cancel fence；TUI UI-only queue 不拥有执行 runtime；默认 CLI/TUI/ACP 路径不调用 `stream_agent` 或 `agent.ainvoke`。
- legacy `ui.stream.stream_agent` 保留为兼容 utility；默认 CLI/TUI/ACP 路径不用它。
- residual/local artifacts note：未跟踪的 `.sessions.sqlite` 与 `transcript.sqlite` 未删除，属于本地残留，禁止提交。

## S5 门禁结果（实际执行）

- `tests/test_runtime_service_routing_s5.py`：24 passed（含本轮 sessions export/routing import boundary 及跨 manager turn quota isolation）。
- service 与 catalog 回归（S1-S4 service + `test_project_catalog.py`）：168 passed。
- RuntimeManager/SessionRuntime 回归：66 passed。
- 全量：尚未在本轮重跑。
- `uv run --no-sync ruff check .`、`git diff --check`：通过。

```powershell
uv run --no-sync pytest tests/test_runtime_service_routing_s5.py -q
24 passed in 1.49s

uv run --no-sync pytest tests/test_runtime_service_contracts.py tests/test_runtime_service_local.py tests/test_runtime_service_events_s3.py tests/test_runtime_service_lifecycle.py tests/test_runtime_service_artifacts_s4.py tests/test_project_catalog.py -q
168 passed in 7.57s

uv run --no-sync pytest tests/test_runtime_manager.py tests/test_session_runtime.py -q
66 passed in 4.20s

uv run --no-sync ruff check .
All checks passed

git diff --check
passed
```

本轮未重跑全量 pytest；不推进 S6。

## S10 消费者迁移（实现完成，最终门禁待完成）

> 状态：`in_progress / implementation complete, final gates pending`。实现已完成，不能提前标记为 `completed`。

- **CLI**：完成 `LocalProjectRuntimeConsumer`；提交前 watch、DTO result、同 loop owner close、cancel fence 均已落地。默认执行链不调用 `stream_agent` 或 `agent.ainvoke`。
- **ACP**：完成 service-only `ACPManagedSession`，不持有 manager/runtime/handle；approval 使用 `PendingApprovalQuery`/`ResumeTurnCommand`，approval wire/client 已接入；checkpoint copy/delete 为纯 callback。安全 ACP 进程内专项 135 passed，B2 50 passed。
- **TUI**：完成 `TUIRuntimeSessionFacade` 与 `RuntimeEvent` renderer。submit/cancel/steer/approval/watch/status/session switch/dialogs/chrome/steer 均通过 service DTO；`TurnController` 不再拥有 execution runtime，compat aliases 仅保留兼容边界。C2 433 passed；C1 主流程阶段证据包括 337、192 passed 等，作为阶段证据记录。
- **ACL**：扩展 13 个 ports；S6 更新 78 passed；approval service/transport 37 passed。
- **唯一执行链**：`AgentRuntimeService -> RuntimeManager.submit_ref/resume_ref -> SessionRuntime -> AgentTurnRuntime`。Local consumer 是 composition owner，不是第二业务 runtime；UI/ACP 不访问 `owner.manager`。
- **事件与关闭**：watch detach/connection close 不 cancel turn；事件使用 session sequence；legacy `ui.stream.stream_agent` 可作为兼容 utility 存在，但 CLI/TUI/ACP 默认路径不使用。

### S10 安全边界与最终门禁

按安全约束，最终验证不得执行或声称执行 `tests/test_acp_p1_transport.py`，也不得执行任何含 subprocess/process API 的测试。本记录不把历史误运行当作架构证据；最终全仓安全门禁、`uv run --no-sync mkdocs build --strict`、`uv build` 与 review 仍待完成，因此总体状态保持 `in_progress`。
