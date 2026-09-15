# Web 端架构审查结论

> 文档状态：Active<br>
> 审查范围：`synapse-web-console` 宿主（`src/synapse/web_console/`）+ `web/`（React/TS 前端）+ 其依赖的 `runtime/daemon/`、`runtime/transport/` 与契约生成链<br>
> 明确不含：`synapse-web`（textual-serve 浏览器 TUI）<br>
> 审查方式：只读代码/文档取证；所有结论附 `文件:行号` 证据

---

## 0. 总体判断

Web 端整体采用**高门槛、防御式**设计：契约单向生成（`contract_registry.py` → manifest + TS）、fail-closed 安全、严格 wire 解码、socket generation fencing。方向正确。

不合理点集中在三处：

1. **双进程 relay** 带来的重复职责与安全旁路；
2. **「单一权威」契约未贯彻**到运行时 dispatch；
3. **前端巨型 store + 多状态源**。

---

## 1. 高优先级问题

| 编号 | 问题 | 证据 | 性质 |
| --- | --- | --- | --- |
| H1 | relay 安全守卫 fail-open | `web_console/host.py:317-322, 358-376` | 架构边界脆弱 |
| H2 | 契约「单一权威」名不副实：dispatch 手写 if/elif | `runtime/transport/protocol.py:1065-1156` vs `runtime/service/contract_registry.py:355-372` | 双份映射，DRY 违背 |
| H3 | 前端 God Store（3126 行 / ~275 字段 + 12+ 模块级可变单例） | `web/src/stores/useConsoleStore.ts:1, 114-148, 868-1105` | 复杂度失控 |
| H4 | 契约漂移无门禁：`limits` 只在 JSON，web 页大小常量手写副本无对拍 | `runtime/service/contract_export.py:483-651`；`web/src/runtime-client/types.ts:91-100` | 隐性漂移 |
| H5 | 文档统计漂移无门禁：ADR-S-019 记 22 方法/89 schema/18 capability，实测 42/129/30 | `docs/agent-runtime-service/adr-s-019-contract-freeze.md:94`；`tests/test_runtime_contract_manifest.py` 无字面计数断言 | 文档失真 |
| H6 | 前端 strict 未覆盖应用代码 | `web/tsconfig.app.json:20-23`；`web/.oxlintrc.json:1-9` | 类型安全缺口 |

### H1 relay 安全守卫 fail-open

`RelayProjectScopeGuard` 对**无法解析的帧**、以及**路由位置形状无法解析的帧**一律放行给 daemon，仅在 `all` 模式下做形状检查：

- 类文档自述 **"The guard is *not* fail-closed"**（`host.py:317-322`）。
- `rejection()` 在 `_scoped_request` 返回 `None` 时只 `unreadable += 1` 并 `return None`（放行，`host.py:360-363`）。
- 设计理由（`host.py:295-322`）：daemon 是唯一项目权威，拒绝发生在解析项目之前，`unreadable` 计数器使边界可观测。

**判断**：逻辑上有辩护（见 §4），但「守卫不 fail-closed」在架构上是脆弱边界——安全依赖下游兜底，而非本层。建议加**告警门禁**（`unreadable` 超阈值即告警），不建议改逻辑。

### H2 契约「单一权威」名不副实

`contract_registry.WireMethod` 声明了 `service_method` 字段作为权威映射（`contract_registry.py:355-372`），但真正的方法分派是 `protocol.py:dispatch` 的**手写 if/elif 链**（42 条，`protocol.py:1074-1156`），并非由 registry 派生。

**判断**：这是明确的 DRY / 权威违背——两处映射需手工同步，registry 的 `service_method` 字段实际未被 dispatch 消费。建议让 `dispatch` 从 registry 派生（如 `getattr(service, wire_method.service_method)(dto)`）。

### H3 前端 God Store

- `useConsoleStore.ts` **3126 行**、`ConsoleStore` 接口约 **275 个字段/动作**（`useConsoleStore.ts:573-866`）。
- store 文件外另有 **12+ 模块级可变单例**作为第二状态源：`initPromise`、`authEpoch`、`projectsGeneration`、`runtimeDiagnosticsPromise/Attempted/Epoch`、`sessionEpoch`、`lastAttachedSession/Epoch`、`pendingDeltas/pendingDeltaTimer`、`attachmentLocalSeq`、`cancelledAttachments`、`lastLiveEpoch`、`attachCoverage`（`useConsoleStore.ts:114-148, 868-1105`）。

**判断**：单文件承载全部状态与副作用，模块级单例脱离 store 管理，是复杂度与可测试性的主要来源。建议按域拆分（session / transcript / runtime / attachments / mcp）。

### H4 契约漂移无门禁（limits）

- `limits`(33) 只存在于 JSON manifest，**不渲染进生成 TS**（`contract_export.py:483-651` 不输出 limits；`contract.generated.ts` 中 `limits` 出现 0 次）。
- web 侧页大小是**手写常量副本**并被直接用作 RPC 默认值：`SESSION_LIST_PAGE_SIZE=50`、`SESSION_SEARCH_PAGE_SIZE=50`、`PROJECT_LIST_PAGE_SIZE=100`、`DIRECTORY_LIST_PAGE_SIZE=200`、`HISTORY_PAGE_SIZE=20`（`web/src/runtime-client/types.ts:91-100`；`SynapseRuntimeClient.ts:494-610`）。

**判断**：manifest `limits` 与 web 常量之间**无生成关系、无对拍门禁**，是结构性漂移风险。建议把 `limits` 渲染进 TS 并断言 web 常量等于生成值。

### H5 文档统计漂移无门禁

`adr-s-019-contract-freeze.md:94` 记录（自称「取自实际提交文件」）：methods 22 / schemas 89 / authorization_capabilities 18 / limits 26；实测 manifest 为 42 / 129 / 30 / 33。同源漂移见 ADR-S-019 `:20, :29, :42, :96, :226, :244, :248, :296`、`contract_registry.py:5-9`、`adr-s-017:13`、`decisions.md:164`。

门禁 `tests/test_runtime_contract_manifest.py` 改为「基线子集 + 命名新增」的断言方式（`:465-474, :516-519`），**不含**任何 42/129/30 字面计数断言，故文档数字错误不会被 CI 发现。

**判断**：文档与实现脱节且无门禁保护。建议为关键统计加断言或改为从 manifest 动态生成文档数字。

### H6 前端 strict 未覆盖应用代码

- `tsconfig.app.json` **未开启 `strict`**，`noUnusedLocals`/`noUnusedParameters` 显式为 `false`（`web/tsconfig.app.json:20-23`）。
- strict 只覆盖 `src/runtime-client`（`web/tsconfig.runtime-client.json:12`）。
- `any` 有使用（`SynapseRuntimeClient.ts` 7 处、`liveEventReducer.ts` 3 处、`turnWork.ts` 1 处），且 `.oxlintrc.json` 未启用 `no-explicit-any`。

**判断**：协议核有严格约束，但应用/组件层缺类型安全网。

---

## 2. 中优先级问题

| 编号 | 问题 | 证据 |
| --- | --- | --- |
| M1 | 双进程 relay 架构：host 与 daemon 为独立 OS 进程，token 经 header 二次传递，认证/连接生命周期职责重叠 | `runtime/daemon/launcher.py:188-233`；`web_console/host.py:844-935` |
| M2 | relay 无重连、无总体读超时（`ClientTimeout(total=None)`），断链即结束，重连责任甩给浏览器 | `web_console/host.py:872-874, 917-932` |
| M3 | web_console 跨层依赖 runtime 内部（`daemon.auth/launcher/config`、`transport.protocol`），架构边界测试未约束 `web_console→runtime` | `web_console/host.py:47, 269, 395`；`web_console/entry.py:14`；`tests/test_runtime_architecture_boundaries.py:103-104` |
| M4 | 重复/派生状态并存：`sessionTitle` vs `sessions[].title`、`usage` vs `sessionUsage`、`gitBranch` vs `gitStatus`、MCP 三态 | `web/src/stores/useConsoleStore.ts:644-648, 740-764, 814-821, 1371-1411` |
| M5 | 双导入路径：`src/client/*` 与 `stores/recoveryDecider.ts` 为 re-export 壳，组件同时从 `client/*` 与 `runtime-client/*` 导入 | `web/src/client/SynapseRuntimeClient.ts:13`；`web/src/components/ArtifactsPanel.tsx:18-39` |
| M6 | 脆弱严格相等校验：`RECOVERABILITY_FIELDS` 要求键集合完全相等，服务端加任一字段即判畸形并降级 | `web/src/runtime-client/recoverability.ts:19-34, 74-78` |
| M7 | 测试覆盖缺口：无任何 `.tsx` 组件测试；3126 行 store 仅 4 个套件触碰；CI 只跑 3/65 个 web 套件，7 个 `*.verify.ts` 不进 CI | `.github/workflows/ci.yml:88-94` |
| M8 | 常量重复与判活不一致：`LOOPBACK_HOSTS` 两处各一份；「daemon 是否在跑」两套判据 | `web_console/config.py:22` / `daemon/launcher.py:34`；`launcher.py:121-129` vs `config.py:149-178` |
| M9 | `--check` 非逐字节：用 `read_text()`（universal newline）比较，ADR/progress 声称「逐字节」 | `scripts/export_contract_manifest.py:37-41` vs `adr-s-019-contract-freeze.md:78` |
| M10 | 配置不可达/硬编码：`send_timeout_seconds`/`daemon_timeout_seconds` 无 CLI 出口；`MAX_SESSIONS=8` 不可配置 | `web_console/config.py:49-50, 102-111`；`web_console/security.py:41` |

---

## 3. 低优先级问题

| 编号 | 问题 | 证据 |
| --- | --- | --- |
| L1 | `zustand` 被运行时 import 却声明在 `devDependencies` | `web/package.json:35`；`web/src/stores/useConsoleStore.ts:1` |
| L2 | 客户端版本硬编码 `'0.1.44'` 与 `package.json` 的 `version:"0.0.0"` 不一致 | `web/src/runtime-client/SynapseRuntimeClient.ts:1333-1335` |
| L3 | `index.html` 外链 Google Fonts 且无 CSP；与「仅回环无网络」注释矛盾 | `web/index.html:25-26`；`web/src/main.tsx:4-7` |
| L4 | 生成契约大量成员未被前端消费（`RUNTIME_EVENT_KINDS`/`TypedRuntimeEvent`/大量 `*Query`/`*Command`），`CONTRACT_VERSION` 运行时未消费 | `web/src/runtime-client/contract.generated.ts:1747, 1788` 等 |
| L5 | `web_console/__init__.py` re-export `host` → 仅 import 包即加载 aiohttp | `web_console/__init__.py:34-39`；`web_console/host.py:45` |
| L6 | 无虚拟滚动，长 transcript 依赖 memo 增量渲染；`dist` 存在 1.46MB mermaid 分块 | `web/src/components/Transcript.tsx:74-97, 986` |
| L7 | 巨型文件群：`SynapseRuntimeClient.ts` 1427、`Transcript.tsx` 1039、`ArtifactsPanel.tsx` 789、`liveEventReducer.ts` 632 | 各文件 |

---

## 4. 判断与取舍

**属于真正架构问题（应修）**

- H1、H2、H3、H4/H5 —— 分别对应安全边界、DRY/权威、复杂度、漂移可观测性。
- M3、M4、M5、M7 —— 分层边界、状态单一源、测试可信度。

**设计上有辩护空间（可保留，但需补门禁）**

- H1 relay fail-open：`host.py:295-322` 给出完整论证——daemon 是唯一项目权威、拒绝发生在解析项目之前、`unreadable` 计数器使其可观测。问题不在逻辑而在「守卫不 fail-closed」的架构定位，建议加告警门禁而非改逻辑。
- M1 双进程：`docs/web-console/formal-host.md` 有明确进程模型论证（host 不拥有 daemon），属刻意的可分离部署设计。

**最值得先动的三件事**

1. 让 `dispatch` 从 `contract_registry` 派生（消除 H2 双份映射）；
2. 把 `limits` 渲染进 TS 并对拍 web 页大小常量（消除 H4）；
3. 拆分 `useConsoleStore.ts` 并消除模块级单例（消除 H3）。

---

## 5. 证据索引（关键文件）

| 主题 | 文件 |
| --- | --- |
| Web Console 宿主 | `src/synapse/web_console/host.py`、`entry.py`、`security.py`、`config.py` |
| Daemon | `src/synapse/runtime/daemon/application.py`、`auth.py`、`launcher.py`、`lease.py`、`config.py` |
| Transport | `src/synapse/runtime/transport/protocol.py`、`websocket.py`、`client.py` |
| 契约 | `src/synapse/runtime/service/contract_registry.py`、`contract_export.py`、`scripts/export_contract_manifest.py` |
| 前端 | `web/src/runtime-client/`、`web/src/stores/useConsoleStore.ts`、`web/src/components/` |
| 文档 | `docs/web-console/formal-host.md`、`docs/agent-runtime-service/adr-s-019-contract-freeze.md` |
