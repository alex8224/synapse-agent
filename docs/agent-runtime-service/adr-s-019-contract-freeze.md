# ADR-S-019：事件与 DTO 契约冻结（v1）

- 状态：Accepted
- 日期：2026-09-11
- 范围：把 `synapse.runtime.service` 的 application DTO ports 与事件模型（现位于 `service/event_types.py`，`runtime/streaming` 只 re-export）正式登记为 **v1 对外契约**，定义单向生成的机器可读 manifest / TS 生成物、兼容规则与门禁。
- 不包含：附件支持、daemon 授权模型改造（属 ADR-S-014 后续）、任何外壳的迁移、wire v2。

## Context

桌面 GUI 是继 TUI、ACP、web console 之后的第四个外壳，也是第一个需要完整管理能力（多项目、多会话、附件、成本）的消费者。在此之前必须先把契约形状固定，否则第二个消费者会把临时决策固化成永久契约。

冻结前的实际状态（均为代码事实）：

> 下表是**冻结前**（本 ADR 决策时）的快照，保留作对照。实施后的真实状态见「实施状态与验证」；其中「删除影子 kind 分支」等原计划已按实施结论修正（见 Decision 2、Consequences）。

| 项 | 现状 | 证据 |
|---|---|---|
| 契约来源 | 三个来源手工同步：service DTO、wire 方法表、web TS 类型 | `service/ports.py:123-221`、`transport/protocol.py:104-127`、`web/src/client/types.ts` |
| 事件 payload | 开放 JSON（`JSONValue`），消费者只能防御性 `.get()` | `service/events.py:300`、`ui/turn/event_renderer.py:134-237` |
| 协议能力协商 | `CAPABILITIES` 是硬编码 4 个协议功能 flag，与 18 个授权 capability 及 24 个方法互不关联 | `transport/protocol.py:82-87` |
| 影子 kind | 消费者接受 `warning` / `thinking_started|completed|updated|finished` / `tool_delta`，枚举中不存在，且**无生产者** | `ui/turn/event_renderer.py:140-211`、`web/src/stores/liveEventReducer.ts:369` |
| 跨端类型漂移 | web `RuntimeEvent.timestamp` 在 Python 侧不存在；web 类型缺 `version` | `web/src/client/types.ts:150` vs `service/events.py:296-301` |
| 契约门禁覆盖 | `CONTRACT_FILES` 漏 `recovery.py` / `artifacts.py` / `access.py`；`events.py` → `runtime.streaming`、`artifacts.py` → `runtime.tool_ignore` 是未受检查的跨包耦合 | `tests/test_runtime_architecture_boundaries.py:85-101` |

版本常量已存在且一致：`EVENT_VERSION = 1`（`runtime/streaming/events.py:9`）、`RUNTIME_WIRE_VERSION = "1"`（`runtime/transport/protocol.py:76`）。

## Decision

1. **契约来源唯一（权威 = 契约层 DTO + `contract_registry.py`，生成物只是快照）**。已落地的权威是 `service/{ports,queries,commands,events,errors,runtime_config,history,recovery,artifacts}.py` 与 `service/event_types.py` 的公开 DTO / 事件 dataclass，加上 `service/contract_registry.py`：它登记 24 个 wire 方法（22 service + 2 transport）的方法名 → service 方法 / 请求 DTO / 结果 DTO / 授权 capability / route scope 映射、24 个 kind → payload schema、4 个协议功能 flag、以及从 `service/access.py` re-export 的 18 个授权 capability 与 schema 清单。`transport/protocol.py` 从该 registry **派生** `METHODS` / `CAPABILITIES`（`transport/protocol.py:54-58`），`scripts/export_contract_manifest.py` 经 `service/contract_export.py` 渲染两个生成物（Decision 10）。依赖方向严格单向：契约层运行时不读取任何生成物，生成物不可能成为第二权威。
2. **事件契约 v1 = 24 个 kind 全量纳入**，payload 采用 **per-kind schema 表**（不是开放映射）：

   | 分类 | kind | 处理 |
   |---|---|---|
   | 纳入 v1（21） | `activity_started` `activity_updated` `activity_stopped` `reasoning_delta` `reasoning_completed` `answer_delta` `answer_completed` `tool_batch_started` `tool_batch_finished` `tool_started` `tool_updated` `tool_finished` `tool_result` `subagent_status_changed` `usage_updated` `approval_required` `info` `turn_completed` `turn_cancelled` `turn_waiting_approval` `turn_failed` | 形状冻结 |
   | 冻结但 UI 忽略（3） | `plan_updated` `plan_removed` `diff_updated` | 纳入 v1；TUI 与 web 明确不渲染，ACP 消费（`acp/updates.py:103-128`） |
   | 语义标注 | `tool_result` = legacy 但三端仍在渲染，**不标记 deprecated**；`subagent_status_changed` = transient，不持久、可缺失 | 契约中显式写明 |
   | 不纳入 v1 枚举 | 影子 kind `warning` / `thinking_*` / `tool_delta` | 不在 `TurnEventKind` 枚举、也不在 manifest 的 24 个 kind 声明中；消费者侧的历史兼容分支**保留**（`ui/turn/event_renderer.py`、`web/src/stores/liveEventReducer.ts`），本 ADR **不承诺删除**它们。未来新增 kind 由「忽略未知 kind」规则覆盖 |

3. **per-kind payload schema 登记**。每个 kind 的 payload 字段名、必填性、语义写入 manifest（形状来源即 `service/event_types.py` 的 frozen dataclass，`runtime/streaming/events.py` 原样 re-export 同一批对象）。**注意 frozen dataclass 只冻结属性赋值，不在运行时校验字段类型**（`dataclass` 不检查注解）：manifest 的「类型」列是从注解静态提取的契约声明，需由 producer fixture 与边界用例验证，而非由 dataclass 运行时保证。运行时不做 per-event 校验（见 Rejected Alternatives）。manifest 登记 89 个 schema（其中 17 个是 `origin: "transport"` 的手工声明）外加 4 个 `standalone_schemas`；门禁断言「方法/事件引用的 schema 全部可达」，但这**不表示所有 schema 的业务语义已被穷尽描述**：字段含义、枚举取值与状态机仍以实现与测试为准。
4. **`info` payload 冻结为裸 `str`**。现状即 `runtime/streaming/adapters.py:267` 的 `str(message)`；v1 冻结其**当前形状**（字符串），不引入 `InfoPayload`。若未来需要结构化，按 additive 规则新增可选字段或新 kind，不得改现有形状。
5. **`RuntimeEvent.turn_sequence` 保留在契约中**。它是 turn-local 序号，与 session `sequence` 游标并列（ADR-005），现有 wire 投影与消费者已依赖该字段；v1 冻结其存在。若判定冗余，只能在 v2 移除。
6. **DTO 契约 v1 = 22 个端口方法 + 全部请求/结果 DTO**（`SessionRef` 作为共享值对象一并入册）。wire 的 24 个方法 = **22 个服务方法（含 `runtime.events.watch` 映射）+ 2 个连接态方法**：
   - `runtime.events.watch` 映射 `AgentRuntimeService.watch_events`，授权 capability `events.watch`，属 22 个服务方法之一。
   - `runtime.protocol.negotiate`：无副作用、无授权 capability，属连接态。
   - `runtime.events.unwatch`：只影响自身订阅、无授权 capability，属连接态。
   `negotiate` 与 `unwatch` 在契约中定位为**传输契约**，不冒充 service DTO。
7. **两层「能力」必须分开，不得混用**：
   - **协议功能 flag（4 个）**：`legacy_v1` / `raw_cursor` / `watch_resume` / `approval_resume`，权威定义在 `service/contract_registry.py` 的 `PROTOCOL_FEATURES`，`transport/protocol.py` 从它派生 `CAPABILITIES`。它们是 `runtime.protocol.negotiate` 返回的 `capabilities` 字段内容，协商语义不变。客户端只把 v1 的这 4 个 flag 当作**必需**（`transport/client.py` 的 `_REQUIRED_PROTOCOL_FEATURES` 与 web 的 `REQUIRED_PROTOCOL_FEATURES` 都是显式字面量，刻意不从 registry 派生），因此 registry 新增的 flag 不会自动变成必需；额外 flag 保持 additive。
   - **授权 capability（18 个）**：`service/access.py` 的 `ALL_RUNTIME_CAPABILITIES` 仍是常量真源，registry 只 re-export；「wire 方法 → capability」映射与它一并冻结（门禁用 AST 独立从 `access.py` 抽取 `method → capability` 再与 registry 对拍，而不是互相信任）。
   两者当前互不相交：4 个 flag 不参与授权，18 个 capability 不出现在 negotiate 响应（门禁断言两个集合无交集）。同时如实记录：daemon 部署使用 `DaemonAuthorizer`，它只校验 `principal.subject == "runtime-daemon"`、从不查询 `AclGrant`，因此 **18 个授权 capability 在 daemon 部署下全部实际放行**（`service/access.py` 的 `DaemonAuthorizer`）。契约冻结的是名称与映射；是否真正生效由 authorizer 决定，属 ADR-S-014 的后续范围，本 ADR 不改变该默认策略。
8. **排除清单**（明确不进契约）：

   | 类型 | 排除原因 |
   |---|---|
   | `Principal` / `AclGrant` / `AccessRequest` / `AclAuthorizer` / `DaemonAuthorizer` / `bind_access` | 进程内安全装配 |
   | `AgentRuntimeService` / `EventStream` / `EventWatch` 协议对象 | 进程内端口与异步租约；wire 用 `subscription_id` 表达 |
   | `SessionEventEnvelope`（包裹内部 `TurnEvent`）/ `SessionEventWindow` / `BrokerRecoveryState` | 泄漏 broker 内部量与 turn-local sequence |
   | `ProtocolError` / `WireProjectionError` 等 Python 异常类型 | 实现细节，不是线上形状 |
   | （**不排除**）`JsonRpcRequest` / `JsonRpcResponse` / `JsonRpcNotification` / `JsonRpcError` / `EventNotification` / `EventNotificationMeta`（`standalone_schemas`）与 `WatchSpec` / `Negotiation` / `NegotiateResult` / `UnwatchRequest` / `UnwatchResult` 等 | 没有 service DTO，但**出现在线上**，因此在 manifest 中以 `origin: "transport"` 手工声明；`JsonRpcRequest` / `JsonRpcResponse` / `JsonRpcNotification` / `EventNotificationMeta` 另列入 `standalone_schemas` |
   | `Local*` 实现类 / `RuntimeProject` / `RuntimeManagerRouter` / `ManagerFactory` / `ProjectProvider` / `_FilesystemContext` / `_ProjectionRejected` / `_BuildState` / `_ShutdownState` | 实现与组合根 |
   | `SubmitTurnCommand.attachments` | v1 显式声明不支持附件（wire 已拒绝非空，`transport/protocol.py:551-553`） |
   | `SubmitTurnCommand.config_overrides` 嵌套值 | 字段在册，但只声明为 `wire: "restricted"`：wire 接受 JSON 对象、不承诺键值 schema，值保持 in-process 对象 |
   | （**不排除**）`WatchSpec.queue_size` / `scan_limit` 系列 | 作为 wire 参数/契约模块有界常量在册（`WatchSpec.queue_size` 记录 `wire default 128, range 1..4096`；`limits` 记录 `default_scan_limit` / `min_scan_limit` / `max_scan_limit`） |

9. **版本与兼容规则**：`contract_version = 1`，与 `EVENT_VERSION = 1`、`RUNTIME_WIRE_VERSION = "1"` 三者对齐（门禁断言三者一致，且 `SUPPORTED_WIRE_VERSIONS == ("1",)`）。只允许 additive 演进：

   | 变更 | 允许 | 规则 |
   |---|---|---|
   | 新增 kind | 是 | 消费者必须忽略未知 kind |
   | 新增 payload 字段 | 是 | 必须可选且有安全默认值 |
   | 新增 wire 方法 | 是 | 必须同时在 manifest 声明 capability |
   | 重命名 / 删除 kind、字段、方法 | 否 | 破坏性变更 → 版本递增 |
   | 改字段类型或语义 | 否 | 破坏性变更 → 版本递增 |
   | 破坏性变更的落地方式 | — | 新版本 + `negotiate` 拒绝旧客户端（`-32002` / `-32003` 语义不变） |

   **同版本内的兼容边界已实现并被 fixture 固定**：未知 `kind` 与未知/额外 payload 字段、以及普通可选字段缺失，在 v1 内被容忍并透传（Python wire 客户端与 web core 各有一组用例）。**不同 event version 不会被自动接受**：web core 的 `classifyRuntimeEvent` 在版本不等时返回 `unsupported_event_version` 并走协议错误路径，Python 客户端只要求 `version` 是非负 int 且不实现其它版本语义。信封缺字段或类型错误一律 fail-closed。

10. **两个生成物都是单向产物**（不是权威来源）：`src/synapse/runtime/service/contract_manifest.json`（语言无关 JSON）与 `web/src/runtime-client/contract.generated.ts`（web 消费的 TS 类型与常量）。两者由 `scripts/export_contract_manifest.py` 经 `service/contract_export.py` 从 registry 渲染，输出确定性（无本地路径、无时间戳、LF 换行），`--check` 模式对提交内容做逐字节校验，漂移或缺失即失败。手工编辑生成物视为失败。
11. **门禁 T1–T7** 见下节。已落地的边界收口：
    - `CONTRACT_FILES` 补齐 `recovery.py` / `runtime_config.py` / `artifacts.py` / `access.py` / `event_types.py`；契约层禁止 import `runtime.transport`、`runtime.streaming`、`runtime.tool_ignore`、`runtime.sessions.{runtime,manager,persistence}`、`service.{local,routing,history_store}`。
    - `events.py` 对 `TurnEventKind` 的依赖：kind 枚举与全部 payload dataclass 已**上移到契约层**的 `service/event_types.py`；`service/events.py` 与 `runtime/streaming/events.py` 都引用它，`streaming` 侧原样 re-export 同一批对象（不是副本）。
    - `artifacts.py` 对 `ToolIgnoreMatcher` 的依赖：已拆为**纯 DTO** `service/artifacts.py`（`ArtifactRef` / 元数据 / 查询 / 校验器与 limit 常量）与 **FS 实现** `service/artifact_filesystem.py`（注入 `ToolIgnoreMatcher` 并做有界 stat/list/read）。契约层只 import DTO 模块。
    - import 闭包用**懒加载**而非「不触发 `__init__`」来收口：Python 导入子模块必然先导入父包，所以 `service/__init__.py` 一定会执行；它已改为 PEP 562 的模块级 `__getattr__` 懒 re-export，`__init__` 本身不再 import `service.local` / `service.routing`。门禁用独立解释器子进程断言「只 import 具体契约子模块或裸 `synapse.runtime.service` 时，闭包内不出现实现模块 / 会话执行栈 / streaming / tool_ignore / langchain 等」。

## Manifest 结构

生成物有两份，都由 registry 渲染：

| 生成物 | 路径 | 用途 |
|---|---|---|
| JSON manifest | `src/synapse/runtime/service/contract_manifest.json` | 语言无关校验；CI `--check` |
| TS 模块 | `web/src/runtime-client/contract.generated.ts` | web console core 消费的类型与版本常量 |

顶层键（取自实际提交文件）：`contract_version`、`wire_version`、`event_version`、`methods`(22)、`transport_methods`(2)、`transport_notifications`(3)、`events`(24)、`schemas`(89)、`standalone_schemas`(4)、`type_aliases`(3)、`protocol_features`(4)、`authorization_capabilities`(18)、`approval_decision_kinds`(4)、`limits`(26)。

下面是**结构真实但内容截断**的节选（字段名、嵌套层级、类型照抄提交文件，只是不列全 24 个方法 / 89 个 schema）：

```json
{
  "contract_version": 1,
  "wire_version": "1",
  "event_version": 1,
  "methods": [
    {
      "method": "runtime.artifacts.list",
      "class": "service",
      "service_method": "list_artifacts",
      "request": "ListArtifactsQuery",
      "result": "ArtifactPage",
      "result_nullable": false,
      "capability": "artifacts.list",
      "scope": "session",
      "scope_location": "params.session",
      "wire_defaults": {"limit": 100, "path": "."},
      "notes": ["``cursor`` is optional; ``limit`` is bounded to 1..1000 by the wire decoder."]
    }
  ],
  "transport_methods": [
    {
      "method": "runtime.protocol.negotiate",
      "class": "transport",
      "params_alias": "NegotiateParams",
      "request": "Negotiation",
      "result": "NegotiateResult",
      "result_nullable": false,
      "capability": null,
      "scope": "connection",
      "scope_location": null,
      "wire_defaults": {},
      "in_process": "Connection-state method: it has no service DTO, no authorization capability, and it is idempotent only for the identical proposal.",
      "notes": ["An unsupported proposal answers -32002 and a second, different proposal", "answers -32003; the response never carries authorization capabilities."]
    }
  ],
  "transport_notifications": [
    {
      "method": "runtime.event",
      "params": "EventNotification",
      "notes": ["One live event per notification; ``cursor`` is the session cursor after it."]
    }
  ],
  "events": [
    {
      "kind": "info",
      "payload": "str",
      "status": "v1",
      "legacy": false,
      "transient": false,
      "notes": ["Payload is a bare JSON string in v1 (the producer sends str(message)); no", "InfoPayload wrapper is introduced."]
    },
    {
      "kind": "plan_updated",
      "payload": "PlanPayload",
      "status": "v1-ui-ignored",
      "legacy": false,
      "transient": false
    }
  ],
  "schemas": {
    "TextPayload": {
      "origin": "python",
      "role": "result",
      "fields": [
        {
          "name": "text",
          "python_type": "str",
          "ts_type": "string",
          "nullable": false,
          "optional": false,
          "required": true,
          "wire": "json"
        },
        {
          "name": "message_id",
          "python_type": "str | None",
          "ts_type": "string | null",
          "nullable": true,
          "optional": false,
          "required": true,
          "wire": "json",
          "default": null,
          "default_kind": "value"
        }
      ]
    }
  },
  "standalone_schemas": ["EventNotificationMeta", "JsonRpcNotification", "JsonRpcRequest", "JsonRpcResponse"],
  "type_aliases": {
    "HistoryToolCall": "Record<string, JsonValue>",
    "HistoryToolResult": "Record<string, JsonValue>",
    "McpServerState": "McpServerStateView"
  },
  "protocol_features": {
    "legacy_v1": true,
    "raw_cursor": true,
    "watch_resume": true,
    "approval_resume": true
  },
  "authorization_capabilities": [
    "artifacts.list", "artifacts.read", "artifacts.stat", "events.read", "events.watch",
    "project.thinking", "session.close", "session.list", "session.mcp.reload",
    "session.open", "session.read", "session.rebind", "session.thinking",
    "turn.approval.read", "turn.approval.resume", "turn.cancel", "turn.steer", "turn.submit"
  ],
  "approval_decision_kinds": ["allow_once", "allow_always", "reject_once", "reject_always"],
  "limits": {
    "default_scan_limit": 1024,
    "max_scan_limit": 4096,
    "min_event_bytes": 1024,
    "default_max_event_bytes": 1048576,
    "max_event_bytes": 8388608,
    "max_list_limit": 1000
  }
}
```

字段语义补充：

- `limits` 是**契约模块当前的有界常量**（实现的默认值与合法范围），不是「这些数字永久不变」的保证；只调数值、不改语义按普通实现变更处理。wire 专有边界（帧大小、subscription id、版本 token、MCP include-tool 上限等）仍留在 transport 常量里并逐方法记录。
- `schemas[*].required` / `optional` 表达的是 wire 投影语义（`result` / `value` schema 的字段在投影中总是出现），**不等于 Python 默认值**；每个方法的 wire 默认值单独声明在 `wire_defaults`，并由门禁与手写 decoder 对拍。
- 24 个 kind 的 payload 形状来自 `service/event_types.py` 的 frozen dataclass；dataclass 只约束字段集，不校验运行时类型，类型与边界由 producer fixture 与边界用例保证。

## 门禁

| 编号 | 内容 | 落点 |
|---|---|---|
| T1 | 22 个 service 方法覆盖 `AgentRuntimeService` 端口；wire 表恰为 24 个方法（22 service + 2 transport） | `tests/test_runtime_contract_manifest.py` |
| T2 | `protocol_features` 与 `authorization_capabilities` 分开断言且无交集；registry 的 capability 集合 == `access.py` 的 `ALL_RUNTIME_CAPABILITIES`；`method → capability` 由 AST 从 `access.py` 独立抽取后对拍 | 同上 |
| T3 | 24 个 kind 全部声明 payload schema；payload 样本与声明字段集一致；无 in-process 对象进 schema | 同上 |
| T4 | 生成 TS 覆盖 web console 当前使用的全部类型名；`runtime-client/types.ts` 只 re-export 生成物、不重复声明 wire 形状；`npx tsc -b` 通过 | 同上（`test_generated_typescript_covers_every_web_console_type`）+ `web/tests/runtimeContractFixture.test.ts` + CI `tsc -b` |
| T5 | 同版本未知 kind / 额外字段 / 缺失可选字段按 additive 处理；信封缺字段或版本不受支持 fail-closed | `tests/test_runtime_transport_client_compatibility.py`、`web/tests/runtimeContractFixture.test.ts`、`web/tests/runtimeClientBoundary.test.ts` |
| T6 | AST + import 闭包门禁：`CONTRACT_FILES` 覆盖、契约层禁止 import 实现包、懒 `__init__` 不拉入实现 | `tests/test_runtime_architecture_boundaries.py`、`tests/test_runtime_service_import_purity.py` |
| T7 | 提交的 JSON manifest 与 TS 模块逐字节等于 registry 渲染结果；共享 fixture 是声明 schema 的真实投影 | `test_committed_artifacts_match_the_registry`、`test_shared_fixture_is_a_real_projection_of_the_declared_schemas`、`scripts/export_contract_manifest.py --check` |

CI 在单次 `contract` job（ubuntu、不进 3.12/3.13 × Windows/Linux 矩阵）里跑：`python scripts/export_contract_manifest.py --check` → 定向 Python 门禁（`test_runtime_contract_manifest.py`、`test_runtime_architecture_boundaries.py`、`test_runtime_service_import_purity.py`、`test_runtime_transport_client_compatibility.py`）→ `npm ci` + `npx tsc -b` → `node --test tests/runtimeContractFixture.test.ts tests/runtimeClientBoundary.test.ts tests/sourceGuard.test.ts`。该 job 只运行上述定向门禁；本 ADR 不就项目整体 CI 的测试范围（例如是否在 CI 运行全量 pytest）作政策声明。

## 冻结前必须解决的不一致

| # | 项 | 实际处理 |
|---|---|---|
| 1 | web `RuntimeEvent.timestamp` 在 Python 侧不存在 | 已从契约类型删除；门禁断言 `RuntimeEvent` 无 `timestamp` |
| 2 | web 类型缺 `version` | 已补上（`version` 是信封六字段之一），纳入 T4 |
| 3 | 影子 kind 无生产者 | 不进入 `TurnEventKind` / manifest；消费者侧历史兼容分支**保留**（见 Decision 2）。原「删除分支」计划已撤销 |
| 4 | `info` payload 裸 `str` | 冻结为裸 `str`，保持现状形状（不引入 `InfoPayload`） |
| 5 | `CAPABILITIES` 硬编码 4 个协议功能 flag | 冻结取值；与 18 个授权 capability 分开登记（见 Decision 7），不改协商语义 |
| 6 | daemon 下 18 个 capability 全部放行 | 契约中如实记录；授权生效问题留给 ADR-S-014 后续，不在本 ADR 解决 |
| 7 | `negotiate` / `unwatch` 无 capability | 契约中显式定位为连接态方法，不参与授权 |
| 8 | `ToolBatchPayload.items` 生产者从不填充（恒空元组） | **保留字段**（已是 v1 形状，门禁断言 `items` 仍在）；生产者暂不填充属实现现状，不作为破坏性变更 |
| 9 | `AccessRequest` 无构造调用点；`rebind_session` 未列入 `_REQUIRED_DELEGATE_METHODS` 却无条件调用；`RuntimeConfigView.can_toggle_mcp_global` 恒 `False` | T1 现在断言 registry 覆盖全部 22 个端口方法（含 `rebind_session`）；`AccessControlledAgentRuntimeService.rebind_session` 现已把 `rebind_session` 当作可选 delegate 方法（与 `set_thinking_level` 同类）：先 `_authorize(session, session.rebind)`，再用 `getattr(delegate, "rebind_session", None)` 探测，缺失时抛 `InvalidRequestError("session rebind is unavailable")`——因此 wrapper 层不再是「无条件调用」，未授权的调用者在旧 delegate 上也被拒；`_REQUIRED_DELEGATE_METHODS` 仍是原先 13 个名字的 fail-fast 清单，未扩列（已知且未变的实现现状）；第三项仍是恒 `False` 的预留字段 |
| 10 | `CONTRACT_FILES` 覆盖缺口 + 两条跨包耦合 | 已按 Decision 11 落地（`event_types.py` 上移、`artifacts.py` / `artifact_filesystem.py` 拆分、懒 `__init__`） |

## Rejected Alternatives

- **payload 保持开放映射**：等于不冻结 payload，消费者永远无法获得可靠形状，跨端漂移会继续发生。
- **运行时 per-event schema 校验**：producer 侧形状由 frozen dataclass 的字段集约束（但 dataclass **不校验字段类型**），类型与边界由 producer fixture 与测试保证；运行时校验只增加热路径成本。
- **新增 UI 中性事件投影层**：会与现有 `RuntimeEvent` 并存两套事件模型，并把「消费者不理解形状」的问题用一层映射掩盖；`RuntimeEvent` 的问题是没有被声明为契约，而不是不够中性。
- **让 TUI 迁移到 wire 客户端库**：TUI 已在同一 DTO 契约上（ADR-S-018），它没有网络；迁移只会重做 `ui/turn/` 与 6007 行 TUI 测试，收益为零。
- **手写 TS 类型并靠 review 对齐**：这正是当前漂移的成因。
- **让契约层反向读取 manifest**：会把生成物变成第二权威，形成「生成物 → 源」的循环依赖；manifest 必须是单向产物。
- **从 manifest 自动生成 wire decoder**：decoder 保持手写，因为它同时承担逐字段边界检查与固定错误映射；改为由门禁把声明的 `wire_defaults` / `submit_turn` 规则与手写 decoder 对拍——这已覆盖默认值与关键 submit 规则，但不能替代所有语义验证，也不牺牲可读的错误语义。
- **顺手删除影子 kind 的消费者兼容分支**：分支已无生产者，但删除只会改动 TUI/web 渲染路径；v1 已用「忽略未知 kind」覆盖未来新增，所以选择保留分支、只把它排除在契约枚举与 manifest 之外。

## Consequences

- 新增外壳的成本主要是「UI + 客户端实现」，service 与 wire 无需为每个外壳改动。但「新增能力所有外壳自动可见」**不成立**：每个外壳仍需各自实现渲染/消费，契约只保证形状一致，不保证 UI 呈现。
- TUI 的 `event_renderer.py` 与 web 的 `liveEventReducer` 仍保留 `info` 裸字符串与影子 kind 的历史兼容分支；本 ADR **不承诺**清理它们，也不把清理当作已完成项。
- web console 的 `liveEventReducer` 与 TUI 的 `event_renderer` 消费同一契约，但跨端对齐仍需各自提交，**不承诺 parity 提交消失**。
- 契约冻结后，`DaemonAuthorizer` 全放行这一事实被显式记录，不再需要从代码反推。
- manifest 与生成 TS 已落地并进门禁，但它们冻结的是**形状与映射**：字段业务语义、状态机与错误分类仍以实现与测试为准，schema 清单「可达」不等于「语义已穷尽描述」。

## 实施状态与限定验证

**已实施**（均为代码事实）：

- 权威与生成物：`service/contract_registry.py`、`service/contract_export.py`、`src/synapse/runtime/service/contract_manifest.json`、`web/src/runtime-client/contract.generated.ts`、`scripts/export_contract_manifest.py` 均已落地；`transport/protocol.py` 从 registry 派生 `METHODS` / `CAPABILITIES`。
- 事件契约：`service/event_types.py` 拥有 24 个 `TurnEventKind` 与全部 payload dataclass，`runtime/streaming/events.py` 原样 re-export；`info` 仍是裸 `str`，`RuntimeEvent.turn_sequence` 保留，`ToolBatchPayload.items` 保留。
- 契约层边界：`CONTRACT_FILES` 已补齐；`service/__init__.py` 改为 PEP 562 懒 re-export；`artifacts.py`（DTO）/ `artifact_filesystem.py`（FS）已拆分。
- web：`web/src/runtime-client/` 是 DOM-free core（`SocketLike` 可注入；默认 `defaultSocketFactory` 在调用时经 `globalThis` 运行时守卫读取宿主 `WebSocket`，不直接引用浏览器 `WebSocket` 绑定，因此无需 DOM libs 即可类型检查/加载；无 `WebSocket` 的宿主必须注入 `socketFactory`），`web/src/client/*.ts` 保留为旧路径 re-export 薄壳；`types.ts` 只 re-export 生成物。UI 文案与 store reducer **未全部迁移**，这不是已发布的 SDK（`web/package.json` 为 `private`），也不支持桌面进程管理。
- 门禁与 CI：`tests/test_runtime_contract_manifest.py` 等四组定向 Python 门禁 + 三个 `node --test` 文件 + `tsc -b` 已进入 CI 的 `contract` job；共享 fixture 为 `tests/fixtures/runtime_contract/fixtures.json` 与 `tests/fixtures/runtime_contract/v1/`，由 Python 与 TypeScript 两侧共同消费。
- TUI/daemon/web host 既有语义保持不变：TUI 侧 `LocalProjectRuntimeConsumer` 仍是线程 owner（`rebind_agent_threadsafe` / `close_session_threadsafe` + 同 loop 阻塞拒绝），daemon 仍提供可注入的 `authenticator_factory` / `authorizer_factory` 且默认 `DaemonAuthorizer` 全权策略不变，浏览器宿主的 project allow-set 与 CSRF 链路不变。

定向验证命令（不跑全量）：

```powershell
uv run --no-sync python scripts/export_contract_manifest.py --check
uv run --no-sync pytest tests/test_runtime_contract_manifest.py tests/test_runtime_architecture_boundaries.py tests/test_runtime_service_import_purity.py tests/test_runtime_transport_client_compatibility.py -q
Push-Location web; try { npx tsc -b; node --test tests/runtimeContractFixture.test.ts tests/runtimeClientBoundary.test.ts tests/sourceGuard.test.ts } finally { Pop-Location }
```

文档结构变化时再跑 `uv run --no-sync mkdocs build`。

## 后续边界

本 ADR 只冻结形状、兼容规则与门禁。以下**未实现，也不声称完成**：

- 附件支持：`SubmitTurnCommand.attachments` 仍被 wire 拒绝非空数组。
- 契约补全：`project.list`、会话 rename / delete 等管理方法未进 wire 方法表（现有 22 个 service 方法不含它们）。

> 后续 additive 记录（不修改上面的冻结结论）：`runtime.project.list` 已按 v1 的
> additive 规则新增（wire 表 25 个方法、`project.list` 为第 19 个授权 capability、
> 新增纯 DTO `ListProjectsQuery` / `ProjectListItem` / `ProjectListPage`）；v1 的 24
> 个方法与 18 个 capability 仍是基线子集，门禁以「基线子集 + 显式新增清单」断言，
> 不改写历史基线。可信连接 scope（宿主私有握手头 + 只做减法的限定 ACL wrapper）与
> Web 改走共享 runtime client 同批完成；会话 rename / delete、附件、远程身份仍 pending。
- 远程身份管理：多用户 / 远程 principal 管理与授权模型改造属 ADR-S-014 后续。
- artifact 写入、wire v2、桌面进程管理。
