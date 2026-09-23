# 动态 Workflow 实施进度

> 本文件是实施状态的单一真源。方案与范围见 [index.md](index.md)，阶段文档不维护状态。
> 状态值：`not_started`、`in_progress`、`blocked`、`completed`。
> 当前总体状态：`completed`（W1—W7 全部完成；已知的既有失败与残余风险见下）。

## 当前工作

- 当前阶段：W7（真实模型端到端、文档与打包门禁）——`completed`。
- 当前任务：无进行中任务。
- 当前阻塞：无。

## 阶段状态

| 编号 | 交付 | 状态 | 完成证据 |
|---|---|---|---|
| W1 | 契约、持久记录、稳定调用身份与 SDK | completed | `tests/test_workflows.py`、`tests/test_workflows_sdk.py`：44 passed |
| W2 | LangGraph worker + 假执行器：进程终止后已完成任务不重跑 | completed | `tests/test_workflows_runner.py`、`tests/test_workflows_worker.py`：19 passed（含真实子进程终止/重启） |
| W3 | 真实 actor 适配（角色解析、上下文隔离、按调用 schema） | completed | `tests/test_workflows_coordinator.py`、`tests/test_workflow_actor.py`：31 passed（含真实图构建与结构化输出） |
| W4 | 调用记录与恢复核对（提交缺口与审批复用） | completed | `tests/test_workflows_recovery.py`：15 passed |
| W5 | 并发、工作区协调、取消与用量 | completed | `tests/test_workflows_service.py`、`tests/test_workflow_turn_gate.py`：15 passed（含真实 worker 子进程生命周期） |
| W6 | Runtime 接口与 Web 视图 | completed | `tests/test_runtime_service_workflows.py` 10 passed；契约门禁 `--check` 通过；web `tsc -b` + 契约/边界守卫 22 passed |
| W7 | 真实模型端到端与回归、文档、打包 | completed | `scripts/workflow_e2e.py` 实跑通过（`deepseek-v4-flash`，3597+34 tokens）；`tests/test_workflows_e2e.py`（默认跳过）；`uv build` 成功 |

依赖顺序：W1 → W2 → W3 → W4 → W5 → W6 → W7。W6 的假数据原型可与 W2 并行设计，
真实数据接入必须等 W3/W4 的调用身份与恢复契约稳定。

## W1 交付明细（completed）

### 新增代码

| 文件 | 内容 |
|---|---|
| `src/synapse/workflows/errors.py` | 领域错误词表（重复 key、重放不一致、结果不确定、预算、审批拒绝等） |
| `src/synapse/workflows/contract.py` | 脚本哈希、调用指纹、`WorkflowLimits`、`DraftStatus`、`WorkflowStatus` 与转移表 |
| `src/synapse/workflows/records.py` | `WorkflowRun`、`CallRecord`、`WorkflowEvent` |
| `src/synapse/workflows/store.py` | SQLite 持久化：草案、运行、调用、事件；硬限制在此集中强制 |
| `src/synapse/workflows/sdk.py` | 脚本侧 API：调用身份、复用校验、schema 校验、审批、进度 |
| `src/synapse/workflows/__init__.py` | 公共导出 |

### 关键设计决定

| 决定 | 理由 |
|---|---|
| 草案状态与运行状态分成两个枚举 | "approved" 是草案状态，不能被当成执行状态展示 |
| 调用指纹不含 attempt 计数 | attempt 属于记录而非身份，否则格式纠正会破坏复用匹配 |
| `create_run(workflow_id, ...)` 从库内读草案 | 调用方持有过期对象也无法启动未经批准的程序 |
| 硬限制在 store 的 `start_call` 内强制 | 调用数与 actor 数不可能被绕过；token 预算另算阈值 |
| 调用被取消时记为 `UNCERTAIN` | 该调用可能已修改文件，不能声称有结果 |
| schema 校验错误只报告约束与位置 | jsonschema 的消息会内嵌模型输出，不能写入事件与日志 |
| 存储写入失败不留半提交结果 | `result_json` 与状态在同一事务内更新 |

### 已验证行为（测试覆盖）

- 调用身份：指纹覆盖角色／actor／提示词／输入／schema／readonly；同一 pass 重复 key 报错。
- 复用：指纹一致时返回历史结果且不再调用执行器；指纹不同报 `ReplayMismatchError`。
- 崩溃窗口：处于 `RUNNING` 的记录被标记为 `UNCERTAIN` 并停止推进；已记录的失败不被隐式重试。
- 结果格式：一次禁用业务工具的格式纠正；两次不合格则调用失败且结果不可复用。
- 限制：`max_calls`／`max_actors` 超限即拒；`max_parallel` 经 `gather` 生效。
- 取消：运行中取消记为 `UNCERTAIN`；取消后不再发起新调用。
- 运行状态机：非法转移被拒；`UNCERTAIN` 不能回到 `RUNNING`。

### 验证命令

```powershell
uv run --no-sync ruff check src/synapse/workflows tests/test_workflows.py tests/test_workflows_sdk.py
uv run --no-sync pytest tests/test_workflows.py tests/test_workflows_sdk.py -q
uv run --no-sync pytest tests/test_runtime_architecture_boundaries.py tests/test_runtime_service_import_purity.py tests/test_subagents.py -q
```

结果：ruff 通过；workflow 测试 44 passed；边界与子代理回归 100 passed。

### 依赖变更

`jsonschema==4.26.0` 提升为直接依赖（此前只在依赖链中），用于宿主侧结果校验；
`uv.lock` 已同步（`synapse-cli-agent` 依赖列表 +2 行）。

## 已核对的技术事实
## W5 交付明细（completed）

### 新增与改动

| 位置 | 内容 |
|---|---|
| `src/synapse/workflows/service.py`（新增） | `WorkflowService` + `WorkflowResources`：草稿、运行、启动/等待/取消、工作区闸门、恢复计划 |
| `src/synapse/app/agent.py` | `build_actor_resources`：actor 需要的模型、后端、检查点、工具与全局排除集 |
| `src/synapse/runtime/sessions/manager.py` | `turn_gate`（新轮次闸门）、`workflow_service`、`close_hooks`（关闭时执行一次，单个失败不阻断其它） |
| `src/synapse/runtime/daemon/application.py` | 每个项目装配 workflow 服务；把闸门与关闭钩子交给 `RuntimeManager`；装配失败降级为"不可用"而不是拖垮普通聊天 |
| `src/synapse/workflows/store.py` | 运行输入持久化（恢复用同一份输入）、调用用量列与迁移、`usage_totals` |
| `src/synapse/workflows/sdk.py` | token 预算按阈值语义执行（达到即拒绝新调用） |
| `src/synapse/app/workflow_actor.py` | 按"本轮新增消息"统计用量，读不到增量就不记账而不是多算 |

### 关键设计决定

| 决定 | 理由 |
|---|---|
| 一个项目同时只有一个活跃运行 | 由 store 强制；服务把同一事实表达为「新轮次暂停」，普通轮次不与工作流抢工作区 |
| 审批续跑不受闸门限制 | 回答待审批不产生新工作，拒绝它会把等待决定的会话卡死 |
| 运行输入持久化 | 恢复必须用同一份输入，否则已记录的调用会被拿去匹配另一个请求 |
| actor 资源懒加载 | 从不跑工作流的项目不应付出模型客户端、shell 后端和检查点的代价 |
| 用量按"本轮新增消息"统计 | actor 会话会累积消息，按整条线程求和会把历史调用重复计入 |
| token 预算只作阈值 | 用量在调用返回后才可知，在途调用仍可能超出；与硬限制区分表述 |
| 装配失败降级 | 工作流数据库打不开不能让该项目失去普通聊天 |

### 已验证行为（测试覆盖）

- 草稿时间戳、按 revision 批准、过期 revision 拒绝；未批准的草稿不能启动运行。
- 运行生命周期：真实 worker 子进程跑完并记录结果与输入；同一项目第二个运行被拒。
- 工作区闸门：工作流活跃时新轮次以冲突被拒并带出运行 id；运行结束后恢复；审批续跑豁免；宽松闸门放行。
- 取消：记录为 `cancelled`，在途调用标为不确定，随后恢复计划被阻断并指出具体调用。
- 已完成的运行再次启动不会产生新派发。
- `close()` 停掉所有运行；关闭钩子只执行一次，且一个失败不阻断其余。

### 验证命令与结果

```powershell
uv run --no-sync pytest tests/test_workflows_service.py tests/test_workflow_turn_gate.py -q
uv run --no-sync pytest tests/test_workflows.py tests/test_workflows_sdk.py tests/test_workflows_runner.py tests/test_workflows_worker.py tests/test_workflows_coordinator.py tests/test_workflows_recovery.py tests/test_workflows_service.py tests/test_workflow_actor.py tests/test_workflow_turn_gate.py -q
uv run --no-sync pytest tests/test_runtime_architecture_boundaries.py tests/test_runtime_service_import_purity.py -q
```

结果：服务与闸门 15 passed；全部工作流测试 **124 passed**；runtime 边界 44 passed。

### 既有失败：已处理

`tests/test_runtime_service_lifecycle.py::test_service_exports_protocol_and_exact_s2_error_codes`
在本次工作开始前即失败：守卫按**子串**匹配 `runtime/service/*.py` 的每一行 import，禁止
`http`/`langchain` 等，而 `service/local.py` 自 v0.1.53 起有两处**函数内延迟导入**
（`httpx` 用于把上游 HTTP 失败归类成有界日志类别；`HumanMessage` 用于模型探测）。

判断：真正要守的性质是**导入期纯净**——`synapse.runtime.service` 必须能在没有 UI、传输层或
模型栈的情况下被导入（这一点由 `test_runtime_service_import_purity.py` 从外部度量）。原守卫
把"延迟导入"也当成违规，属于口径过宽。

处理（两处，都已验证）：

| 改动 | 内容 |
|---|---|
| 去掉一个不必要的依赖 | 模型探测改用普通消息映射 `{"role": "user", "content": "hi"}`，`local.py` 不再 import langchain |
| 收紧并加强守卫 | 改为按 AST 检查：**模块级**导入禁止 UI/CLI/传输/Web 框架/模型栈；**函数内**导入若是重依赖则必须显式登记理由（目前仅 `httpx`），且登记项一旦不再被使用就会失败 |

反例验证：临时插入模块级 `import httpx` → 守卫失败（`imports 'httpx' at import time`）；
临时插入未登记的延迟 `import langgraph` → 守卫失败（`defers a heavy import that is not
declared`）；恢复后通过。

## W6 交付明细（completed）

### 契约层（6 个 wire 方法）

| 方法 | capability | scope |
|---|---|---|
| `runtime.workflow.draft.save` | `workflow.write` | project |
| `runtime.workflow.draft.approve` | `workflow.write` | project |
| `runtime.workflow.run.start` | `workflow.control` | project |
| `runtime.workflow.run.cancel` | `workflow.control` | project |
| `runtime.workflow.run.get` | `workflow.read` | project |
| `runtime.workflow.run.list` | `workflow.read` | project |

| 位置 | 内容 |
|---|---|
| `service/workflows.py`（新增） | 纯 DTO 与视图；不 import 工作流域、传输、设置或执行栈 |
| `service/access.py` | 三个新能力 + 每个方法的项目级授权包装 |
| `contract_registry.py` | 6 个 `WireMethod`、14 个 `_dto` schema 声明 |
| `service/ports.py` | 6 个可选委托方法 |
| `service/local.py` + `service/workflow_projection.py` | 实现与"记录 → 视图"投影；领域错误映射为 `invalid_request` / `conflict` / `not_found` |
| `transport/protocol.py` | 每个方法的解码（含嵌套 limits、边界与默认值）与分发分支 |
| 生成物 | `contract_manifest.json`、`contract.generated.ts` 已重新生成，`--check` 通过 |

### Web

| 位置 | 内容 |
|---|---|
| `runtime-client/workflows.ts` | 严格解码器（只接受声明字段，拒绝半成形对象）、进度计数、状态标签、恢复提示 |
| `runtime-client/SynapseRuntimeClient.ts` | 6 个客户端方法 |
| `stores/workflowView.ts` | 运行列表、选中运行、启动/取消/批准动作；不并入 `useConsoleStore` |
| `components/bottomBar/workflowItem.tsx` + `manifest.tsx` 一行 | 底栏入口与紧凑面板（列表、调用明细、取消） |
| `tests/workflowView.test.ts` | 解码、进度计数与恢复提示的纯函数测试 |

### 两处刻意的"不做"

- **不显示进度百分比**：动态流程会继续展开，只有已派发的调用数是真实进度。
- **不给未知结果的运行提供"继续"按钮**：运行自己给出不能继续的原因，界面只呈现该原因。

### 验证命令与结果

```powershell
uv run --no-sync python scripts/export_contract_manifest.py --check
uv run --no-sync pytest tests/test_runtime_contract_manifest.py tests/test_runtime_service_workflows.py -q
cd web; npx tsc -b
cd web; node --test tests/workflowView.test.ts tests/runtimeContractFixture.test.ts tests/runtimeClientBoundary.test.ts tests/sourceGuard.test.ts
```

结果：契约产物无漂移；契约与线协议测试 31 passed；全部工作流测试 **155 passed**；
web 类型检查通过、守卫与视图测试 28 passed、oxlint 0 error。

## W7 交付明细（completed）

### 真实模型端到端

`scripts/workflow_e2e.py` 用**已配置的默认模型**跑完整路径：保存并批准草案 → 经
`WorkflowService` 启动运行 → worker 子进程发起调用 → 宿主用真实模型执行 actor →
宿主按声明的 schema 校验结果 → 记录用量。

实测结果（默认模型 `deepseek-v4-flash`，`model spec: openai:deepseek-v4-flash`）：

```
outcome: completed
  call review:answer: completed attempts=1 tokens=3597+34 error=-
tokens: 3597+34
result: {'items': ['ok']}
```

`tests/test_workflows_e2e.py` 把它变成可重复的验收：默认**跳过**（需要凭据且消耗 token），
设置 `SYNAPSE_WORKFLOW_E2E=1` 时执行并断言运行完成与结果形状。

### 写路径闭环（真实模型 + 真实文件修改）

`scripts/workflow_write_e2e.py` 覆盖能改动工作区的那条路径，全部在**临时工作区**中进行
（本仓库不被修改）：

1. 建一个带真实缺陷的 `calc.py`（`add` 写成了减法）与其自带自检；
2. 工作流先走业务审批门禁 `wf.approve("fix", …)`，再让 actor 读文件、做最小修复、运行自检；
3. 引擎按 actor 返回的**结构化** `passed` 字段决定是否返工，最多两次；
4. 脚本自己再跑一次自检，独立确认修复真的生效。

实测结果（默认模型 `deepseek-v4-flash`，角色取环境里可写的内置角色 `tester`）：

```
outcome: completed
  call fix:1: completed attempts=1 tokens=14960+342
tokens: 14960+342
result: {"attempts": 1, "passed": true, "summary": "Fixed calc.py by changing add()'s body
         from 'return a - b' to 'return a + b' ... exited 0."}
self-check: rc=0 add(2, 3) = 5
```

也就是说：actor 真的改了文件、真的跑了自检、结构化结论被引擎用作分支依据，且**没有多余的
返工**（attempts=1）。`SYNAPSE_WORKFLOW_E2E=1` 时该脚本也作为第二个可选验收用例运行。

### 实测暴露并修正的两个 provider 约束

| 现象 | 处理 |
|---|---|
| 网关拒绝 `tool_choice: "any"`（`422 invalid_request_error`）——LangChain `ToolStrategy` 固定使用该值 | 不再依赖工具策略 |
| 网关接受 `response_format` 但不产出 JSON（`ProviderStrategy` 解析失败） | 不再依赖 provider 侧结构化输出 |

结论与实现：**声明的 schema 写进提示词，由宿主解析并校验**。这与方案中"传给 provider 的
schema 是请求，不是保证"的原则一致，也是唯一跨 provider 都成立的保证。actor 图因此不再依赖
schema，图缓存改为按（角色, 是否格式纠正）。

### 文档与打包

| 项 | 内容 |
|---|---|
| `README.md` | 新增「Dynamic workflows」一节：用途、代码落点、以及四条使用边界（本机执行无沙箱、批准流程不等于授权其中所有命令、取消不撤销已发生的修改、不显示进度百分比） |
| `docs/workflows/index.md` | 方案与边界（既有） |
| 打包 | `uv build` 成功产出 sdist 与 wheel |
| 契约门禁 | `export_contract_manifest.py --check` 无漂移；web `tsc -b` 与三个守卫测试通过 |

### 验证命令与结果

```powershell
uv run --no-sync python scripts/workflow_e2e.py
uv run --no-sync pytest tests/test_workflows_e2e.py -q            # 默认 skipped
$env:SYNAPSE_WORKFLOW_E2E='1'; uv run --no-sync pytest tests/test_workflows_e2e.py -q
uv build
```

结果：真实端到端通过；全部工作流与契约测试 **155 passed, 1 skipped**。

浏览器端到端复跑后的完整回归：

```powershell
uv run --no-sync pytest tests/test_workflows*.py tests/test_workflow_actor.py tests/test_workflow_turn_gate.py tests/test_runtime_service_workflows.py tests/test_runtime_contract_manifest.py -q
cd web; npx tsc -b; node --test tests/workflowView.test.ts tests/runtimeContractFixture.test.ts tests/runtimeClientBoundary.test.ts tests/sourceGuard.test.ts; npm run lint
```

结果：**160 passed, 1 skipped**；web 类型检查通过、守卫与视图测试通过、oxlint 0 error。

### 浏览器端到端（真实 daemon + Chrome）

用 CDP 在 Chrome 中驱动**真实控制台 + 真实 daemon**（daemon 由本仓库 venv 启动，隔离的
`--state-dir`；用户的 Tauri daemon 未被触碰）。流程：配对 → 新建会话 → 通过控制台自身的
WebSocket 中继创建/批准/启动运行 → 在底栏打开工作流面板 → 查看运行与调用明细 → 取消运行。

实测结果：

| 步骤 | 观察到的结果 |
|---|---|
| 底栏入口 | `workflow: N` 显示本项目运行数 |
| 面板列表 | 运行状态 + 已派发调用数（如 `已完成 run-… 已完成 10 项`） |
| 运行明细 | 运行 id、调用次数、用量（如 `3 次调用 · 用量 6979`）、恢复提示、逐条调用与角色 |
| 取消 | 运行中运行提供「取消工作流」；点击后列表变为 `已取消`，明细显示 `取消中` 与 daemon 给出的原因 |
| 已结束运行 | **不提供**取消按钮（设计如此） |

截图证据：`.synapse/workflow-panel.png`、`.synapse/workflow-panel-cancel.png`（`.synapse/` 不入版本库）。

### 浏览器端到端发现的四个缺陷（均已修复并加测试）

| 缺陷 | 影响 | 修复 |
|---|---|---|
| daemon 装配把 `resolved_sessions_path()`（**文件**）当成目录拼接 | 每个项目的工作流支持都被静默降级为不可用，`runtime.workflow.*` 全部失败 | 改用其父目录；新增 `tests/test_workflow_daemon_wiring.py`（含"不可用时降级而不是抛错"） |
| popover 条目未把触发元素绑定为锚点 | 面板**永远不渲染**（`aria-expanded=true` 却没有任何内容） | `ref={anchorRef}` + `context.toggle(id, event.currentTarget)`，与既有 popover 条目一致 |
| 新 actor 线程被当作"状态不可读" | **每次运行的第一个调用用量记为 0**，而它是最贵的一次（含完整系统提示词） | 空线程视为"0 条消息"而不是未知；新增用量与计数测试 |
| 已结束运行的提示写作 `运行已已完成` | 文案错误 | 改为 `该运行已完成` |

另外补上：面板新增显式「刷新」按钮（不做轮询），因为使用中确实遇到了列表停留在旧状态。

### 一处未完全解释的观察（如实记录）

修复路径缺陷后，第一次通过控制台中继调用仍返回 `invalid_params`，而同一参数**直连 daemon
成功**；把 daemon 与控制台都重启后中继恢复正常。中继本身是逐字转发（且由既有字节一致性测试
守护），因此最可能的原因是宿主持有到旧 daemon 进程的连接；但我没有重现出确定结论，故记录为
观察而非结论。

### 尚未覆盖（如实记录）

| 项 | 说明 |
|---|---|
| 「批准后修复 → 测试 → 有界返工」闭环 | 需要真实修改工作区；验收脚本目前只跑只读的单调用闭环。返工与有界循环由 W1/W2 的测试覆盖（`gather`、`max_calls`、纠正尝试），但未在真实模型下端到端跑过写路径 |
| 经真实 daemon + 浏览器驱动一次 | **已完成**（见上节「浏览器端到端」） |
| TUI 入口 | 未提供；服务契约已就绪，TUI 接入未做 |
| Linux 上的 worker 终止路径 | 代码用进程组信号，未在 Linux 实测（Windows 用 `taskkill /T` 已实测） |
| 写路径闭环 | **已完成**（见上节）；但真实模型下只跑到 1 次尝试，返工分支本身仍由单元测试覆盖 |
| 并发写入的工作区协调 | 设计上同一项目只允许一个活跃运行、普通轮次暂停；未在真实并发场景下压测 |
| 用量口径的其余边角 | 首个调用的用量已修复；但用量来自消息上的 `usage_metadata`，provider 不返回该字段时仍记为 0（不虚构数字） |

## 已核对的技术事实
## W4 交付明细（completed）

### 新增与改动

| 位置 | 内容 |
|---|---|
| `src/synapse/workflows/recovery.py`（新增） | `reconcile_run`（核对并记录未知结果）、`plan_resume`（可否继续）、`ResumeBlocker`、`ResumePlan`、`ReconcileReport` |
| `src/synapse/workflows/runner.py` | 重放脚本**之前**先核对记录并检查恢复计划；被阻断时提前失败 |
| `src/synapse/workflows/sdk.py` | 业务审批复用已记录的决定；新增 `approval.reused` 事件 |
| `src/synapse/workflows/store.py` | `approval_decisions`（按事件日志取每个 key 的最终决定）、`count_calls`（计数校验） |
| `src/synapse/workflows/worker.py` | 失败报告中的 `uncertain` 改为按运行记录判断，而不是只看异常类型 |

### 关键设计决定

| 决定 | 理由 |
|---|---|
| 提交顺序固定为「先 store 记录、后检查点」 | 这一步让最危险的方向不可能出现：副作用已发生却没有记录 |
| 核对**只记录未知结果，绝不编造结果** | 一个 RUNNING 调用意味着"结果未确定"，不是"可以重试" |
| 在途调用被发现时同时把运行标为 `UNCERTAIN` | 有未知结果的运行不是"仍在运行"，否则读状态的人会以为它还在工作 |
| 恢复计划在重放脚本之前判定 | 被阻断的运行不会重跑前导代码，失败原因精确且代价更小 |
| 已批准的业务门禁在恢复后复用，已拒绝的报错，未决的重新询问 | 复用是尊重用户已回答的问题；重新询问人是安全的，替用户假设答案不是 |
| 计数与记录不一致也算阻断 | 撕裂写入不能被当成正常状态继续 |

### 已验证行为（测试覆盖）

- 在途调用被记录为 `UNCERTAIN`（含原因），运行随之变为 `UNCERTAIN`；重复核对不会改写首次原因。
- 已完成的调用不受核对影响；计数不一致会被检出并阻断。
- 恢复计划：干净运行可继续；存在未确定/失败调用、被取消、已终止、计数不一致都被阻断，并给出具体调用 key 与原因。
- `WAITING_APPROVAL` 仍可继续（等待决定不等于结果未知）。
- **被阻断的恢复不重放脚本**：脚本前导代码不执行、执行器零派发。
- 恢复后复用已记录结果：**不重复派发**，且运行调用计数不增加（预算不重复扣）。
- 审批：已批准复用（`approval.reused`，不再询问）、已拒绝报错、未决重新询问。

### 验证命令与结果

```powershell
uv run --no-sync pytest tests/test_workflows_recovery.py -q
uv run --no-sync pytest tests/test_workflows.py tests/test_workflows_sdk.py tests/test_workflows_runner.py tests/test_workflows_worker.py tests/test_workflows_coordinator.py tests/test_workflows_recovery.py tests/test_workflow_actor.py -q
```

结果：恢复测试 15 passed；全部工作流测试 **109 passed**。

### 残余风险（已记录，不再声称是缺口）

- 「检查点已提交、store 记录未提交」这个方向**在本设计中不可能**：store 记录总是先提交。
  可能的只剩「store 已提交、检查点未提交」，而这一方向的恢复是**复用**已记录结果，
  不会重复副作用（已有测试覆盖）。
- 掉电导致 store 提交丢失属于存储层假设；`count_calls` 与运行计数的不一致检查用于检出，
  检出后阻断恢复而不是继续。

## 已核对的技术事实
## W3 交付明细（completed）

### 宿主侧协调器（`src/synapse/workflows/coordinator.py`）

协调器驱动一个 worker 子进程直到运行落定，并且**只在有证据时才宣称某种结局**：

| 情况 | 记录的结果 |
|---|---|
| worker 汇报结果 | `completed`（若运行已是终态则不覆盖） |
| 调用在宿主侧失败 | 该调用 `failed`，运行 `failed`，已提交的其它调用仍可复用 |
| 业务审批被拒或无审批通道 | 运行 `failed` |
| 脚本无法编译（worker 退出码 3） | 运行 `failed`：什么都没执行过 |
| worker 被杀死或异常退出 | 运行 `uncertain`，且**在途调用被标记为结果不确定** |
| 超过 `max_seconds` | 杀死 worker，运行 `failed` 并给出时限原因，在途调用标记不确定 |
| 用户取消 | `cancelling → cancelled`，在途调用标记不确定（可能已改动文件） |

`on_call` 是真实 actor 适配要接入的接缝：协调器本身不认识模型、工具或会话，
因此可以用假执行器完整测试。`process_factory` 同样是可注入的，测试用脚本化
worker 替身验证协议处理（包括忽略未知消息类型而不终止运行）。

同一阶段对 runner 的一处修正：**已经终态的运行不再重新执行脚本**，直接返回已存结果；
终态也不会再被后续结算覆盖。

### actor 适配（`src/synapse/app/workflow_actor.py`）

| 交付 | 说明 |
|---|---|
| 角色解析 | 复用合并后的角色定义（内置 + 用户覆盖 + 禁用名单）；未知或已禁用的角色在派发前拒绝 |
| 工具策略单一来源 | 新增 `resolve_role_tool_policy`，`compile_task_specs`（task 路径）与 actor 路径共用，同一角色不会出现两套策略 |
| 只读只能收窄 | `readonly=True` 只会增加屏蔽项；调用方无法给角色额外授权 |
| 禁止嵌套编排 | `task` 与 todo 工具始终被屏蔽：一个节点若能自起编排，运行就无法记录和恢复它 |
| actor 执行身份 | `wf:<run_id>:<actor_key>` 顶层线程，actor 之间、actor 与用户会话之间互不串扰 |
| 结果格式与缓存 | 按（角色, schema, 是否格式纠正）缓存图配置：`ToolStrategy` 不允许运行期新增未声明的格式 |
| 宿主侧校验 | actor 返回后仍由 `validate_result` 校验；缺失结构化响应按校验失败处理，从而触发一次纠正 |
| 格式纠正 | 纠正尝试额外屏蔽写入与 shell 工具，格式修复无法改动工作区 |
| 提示词一致 | 新增 `build_role_system_prompt`，actor 与 `task` 子代理使用同一提示词构建 |

### 已验证行为（测试覆盖）

- 三种内置角色 × 只读两态：始终屏蔽 `task`/todo 工具；只读只会收窄可用工具。
- 未知角色、被禁用角色、空角色名都被拒绝，且不会构建任何图。
- 只读请求经执行器传递到装配后的 actor（模型请求层可验证）。
- 结果契约：合法结构化结果通过；不合 schema、缺失结构化响应、非状态返回值都报校验失败。
- 无 schema 的调用返回最后一条 AI 文本。
- 图缓存：同（角色, schema）复用；换 schema 或换纠正尝试会重建；不同 actor key 复用图但线程不同。
- **真实图路径**：用脚本化模型构建并运行 actor 图，取回结构化输出并通过宿主校验；
  同时断言模型请求中**不含** `task` 工具，纠正尝试的请求中**不含**写入与 shell 工具。

### 尚未验证（不属于 W3）

| 项 | 归属 |
|---|---|
| 真实模型 provider 的端到端行为 | W7；当前用脚本化模型证明图与契约路径 |
| 由 daemon 提供真实 model/backend/checkpointer 并接入 `RuntimeManager` | W5 |
| actor 用量计入项目配额、取消向 actor 传播 | W5 |
| 内部 actor 会话不出现在普通会话列表 | W6（服务层投影） |

### 验证命令与结果

```powershell
uv run --no-sync pytest tests/test_workflows_coordinator.py -q
uv run --no-sync pytest tests/test_workflows_coordinator.py tests/test_workflow_actor.py -q
uv run --no-sync pytest tests/test_subagents.py tests/test_tool_policy_guard.py tests/test_subagent_policy_integration.py tests/test_subagent_environment.py tests/test_subagent_status.py tests/test_subagent_config_persist.py tests/test_compact_tool_descriptions.py -q
```

结果：协调器 11 passed、actor 20 passed；全部工作流测试 94 passed；子代理与工具策略回归 195 passed。
其中 `resolve_role_tool_policy` 的抽取未改变 `compile_task_specs` 的行为（子代理测试全绿）。

## 已核对的技术事实
## W2 交付明细（completed）

### 新增代码

| 文件 | 内容 |
|---|---|
| `src/synapse/workflows/protocol.py` | worker↔父进程的行分隔 JSON 协议、`WorkerConfig`、调用载荷与检查点路径规则 |
| `src/synapse/workflows/runner.py` | 脚本编译与入口校验、LangGraph `entrypoint`/`task` 装配、运行终态结算 |
| `src/synapse/workflows/worker.py` | 子进程入口：store + 编排检查点、父进程调用通道、审批通道、stdout 独占 |
| `src/synapse/workflows/process.py` | 父进程侧子进程句柄：启动、读写消息、等待、终止与进程树清理 |

SDK 相应变化：`execute_call` 成为公开的原始调用路径（供 LangGraph 任务包装），
`set_call_runner` 安装包装，`finish` 幂等。

### 关键设计决定

| 决定 | 理由 |
|---|---|
| worker **不执行 agent**，每次调用通过管道交给父进程 | 凭据、工具策略、审批、用量留在 daemon；worker 只需运行脚本 |
| LangGraph 任务输入用 JSON 字典而非 dataclass | 检查点要序列化任务输入，字典是框架序列化器保证可往返的形状 |
| 任务包装在固定的模块位置定义 | 任务标识由函数的限定名推导，跨进程必须一致，恢复才能命中已提交结果 |
| `durability="sync"` | 任务结果必须在 worker 汇报前落盘，否则崩溃会丢掉已完成的调用 |
| 编排检查点使用**独立数据库文件** | 实测：与工作流记录共用同一文件时，检查点的写事务会让 store 的写等待超时后报 `database is locked` |
| store 写事务使用 `BEGIN IMMEDIATE` | SQLite 不对锁升级等待，先读后写的延迟事务会立刻 BUSY，`busy_timeout` 形同虚设 |
| 被取消的调用记为 `UNCERTAIN` | 该调用可能已经修改文件，不能声称有结果，也不能盲目重试 |
| 父进程可终止 worker 及其子进程 | 不让出执行权的脚本不能拖住 daemon；这是故障隔离，不是安全边界 |

### 已验证行为（测试覆盖）

- 脚本编译错误、缺少 `run`、`run` 不可调用都在派发前被拒。
- 同步与异步入口都可用；`gather` 的每个调用都有独立记录。
- **中断后不重跑已完成调用**：进程内中断（检查点复用）与真实子进程被杀死后重启（跨进程复用）都有测试。
- 中断时正在执行的调用在重启后被判为结果不确定，且**不会**再次派发给宿主。
- 已经完成的运行再次执行不产生新的调用。
- 不让出执行权的脚本由父进程终止，且不会被报告为完成。
- 非法启动消息以配置错误退出码结束。
- worker 不导入 agent 栈（`deepagents`、`textual`、会话执行栈、`langchain_openai`）。

### 验证命令与结果

```powershell
uv run --no-sync ruff check src/synapse/workflows tests/test_workflows_*.py
uv run --no-sync pytest tests/test_workflows.py tests/test_workflows_sdk.py tests/test_workflows_runner.py tests/test_workflows_worker.py -q
uv run --no-sync pytest tests/test_runtime_architecture_boundaries.py tests/test_runtime_service_import_purity.py -q
```

结果：ruff 通过；工作流测试 63 passed；边界回归 44 passed。

### 本阶段暴露的两个实测约束

1. **检查点与工作流记录不能共用 SQLite 文件**。LangGraph 检查点在任务运行期间持有写事务，
   共用文件时 store 的写入会等到 `busy_timeout` 超时后失败（实测约 7.2 秒后报
   `database is locked`）。已改为独立文件：`<db_path>.checkpoint`。
2. **先读后写的延迟事务在 SQLite 下无法等待锁**。store 的写事务改为 `BEGIN IMMEDIATE`，
   先取写锁再读写，`busy_timeout` 才真正生效；这也是后续 daemon 与 worker 同时写
   同一 store 的前提。

## 已核对的技术事实

这些结论来自阅读已安装依赖与现有源码，是后续阶段的前提：

| 事实 | 影响 |
|---|---|
| LangGraph 1.2.9 提供 `entrypoint`／`task`、并行任务与检查点恢复 | 编排持久化复用框架，不自研重放引擎 |
| 同一图重建 + 同一 saver：中断恢复时已完成任务不重跑 | W2 的可行性基础（内存实验已验证，未验证进程崩溃与 SQLite） |
| LangGraph 任务恢复与调用位置相关，不按业务 key 校验输入 | SDK 必须做调用一致性检查（W1 已实现） |
| 原始 JSON Schema 传给 LangChain 不产生校验 | 宿主必须显式校验（W1 已实现） |
| `ToolStrategy` 不允许运行期加入未在构图时声明的结果格式 | 需要按角色配置 + schema 缓存图配置（W3） |
| 一个 `SessionRuntime` 同时只承载一个活跃 turn | actor 需要独立执行身份，不能共用用户会话（W3） |
| Web 的 `pendingApproval` 是单项状态 | 并行 actor 需要审批列表，不能互相覆盖（W6） |
| `runtime/workspace_changes.py` 是有界展示快照 | 不能用作全仓库一致性证明（W4 需明确核验范围） |

## 未验证与已知风险

| 项 | 状态 |
|---|---|
| 进程崩溃后 SQLite 编排恢复 | 已由 W2 子进程测试覆盖（终止→重启→不重跑已完成调用） |
| worker 死循环终止 | 已由 W2 覆盖；Windows 用 `taskkill /T`，POSIX 用进程组信号，Linux 未在本机实测 |
| actor 与 workflow 记录的提交缺口核对 | W4 验证；当前只有"结果不确定"的处理规则 |
| 重启后重建待审批身份 | 现有 `SessionRuntime` 的待审批依赖进程内状态，需要适配 |
| token 预算的准确口径 | 用量在调用返回后才可知，阈值语义已在文档中明确 |
| TUI 接入 | W6 之后；不能把进程内 TUI 关闭描述成后台继续运行 |
| 提交缺口 | 已由 W4 收口：提交顺序 + 核对 + 恢复计划（见上节），并留下残余风险的明确表述 |
| 真实模型 provider 未跑通 | actor 图与契约路径已用脚本化模型验证；真实 provider 的端到端行为在 W7 |
| 服务契约与 UI 未提供 | 目前只能由 Python API / 测试驱动；W6 未开始，见「剩余工作」 |
| daemon 装配未经真实模型验证 | W5 已接入 `build_actor_resources` 与闸门，但真实 provider 路径要在 W7 验证 |

## 变更文件

- `src/synapse/workflows/`（新增包）
- `src/synapse/app/workflow_actor.py`（actor 解析、图装配与结果契约）
- `src/synapse/workflows/service.py`（项目级工作流生命周期与工作区闸门）
- `src/synapse/runtime/subagent_specs.py`（新增 `RoleToolPolicy` / `resolve_role_tool_policy` / `build_role_system_prompt`，并让 `compile_task_specs` 复用策略计算）
- `src/synapse/runtime/subagents.py`（新增 `resolve_role_definitions`）
- `src/synapse/runtime/sessions/manager.py`（闸门、workflow 服务引用、关闭钩子）
- `src/synapse/runtime/daemon/application.py`（每项目装配 workflow 服务）
- `src/synapse/app/agent.py`（`build_actor_resources`）
- `tests/test_workflows.py`、`tests/test_workflows_sdk.py`、`tests/test_workflows_runner.py`、`tests/test_workflows_worker.py`、`tests/test_workflows_coordinator.py`、`tests/test_workflows_recovery.py`、`tests/test_workflows_service.py`、`tests/test_workflow_actor.py`、`tests/test_workflow_turn_gate.py`（新增测试）
- `tests/test_workflows_e2e.py`（真实模型验收，默认跳过）、`tests/test_workflow_daemon_wiring.py`（daemon 装配与降级）、`scripts/workflow_e2e.py`（可重复的验收脚本）
- `web/src/runtime-client/workflows.ts`、`web/src/stores/workflowView.ts`、`web/src/components/bottomBar/workflowItem.tsx`、`web/tests/workflowView.test.ts`（控制台侧）
- `pyproject.toml`、`uv.lock`（`jsonschema` 直接依赖）
- `docs/workflows/index.md`、`docs/workflows/progress.md`、`mkdocs.yml`（本文档）
