# Agent Runtime 解耦架构决策记录

> 状态说明：`Accepted` 表示当前实施基线；发生变化时追加新决策，不覆盖历史原因。

## ADR-001：采用单进程双 loop，不采用每项目常驻进程

- 状态：Accepted
- 决策：保留 Textual 主线程 loop 和现有进程级 Agent `AsyncRuntime` loop；所有项目运行时共享该 Agent loop。
- 原因：当前进程基线内存较高，每项目进程会重复加载 Python、DeepAgents、模型和中间件；同时还需引入高频流事件 IPC，成本与当前目标不匹配。
- 影响：项目隔离必须通过显式 `ProjectRuntime` 和资源 registry 完成，不能依赖进程边界。
- 重审条件：未来出现无法隔离的原生崩溃、高风险工具或明确的 daemon/RPC 产品需求。

## ADR-002：先解耦 Agent turn，再实现多会话和多项目

- 状态：Accepted
- 决策：P0-P3 只建立 UI-independent turn runtime 和 Textual adapter，不同时引入跨项目。
- 原因：当前 turn 生命周期、事件解析、DOM 更新和持久化交织；直接叠加多项目会扩大状态串线风险。
- 影响：P3 完成前不实现“切换后继续运行”的用户功能。

## ADR-003：Runtime 产生语义事件，TUI 负责展示策略

- 状态：Accepted
- 决策：运行层产生 answer、reasoning、tool、usage、activity 和终态事件；颜色、折叠、限频、DAG task 隐藏、Git chrome 刷新属于 UI renderer。
- 原因：DOM 命令无法供 CLI、测试和未来前端复用，也会让无订阅者运行失效。
- 影响：事件 payload 必须是纯领域数据，不能包含 Textual widget。

## ADR-004：运行结果不能依赖事件消费者状态

- 状态：Accepted
- 决策：answer/reasoning 累积、去重、usage 和最终状态由 runtime accumulator 保存；不能再从 `TextualStreamSink.answer_buf` 推导结果。
- 原因：消费者可能未订阅、丢弃预览事件或在会话切换时被替换。
- 影响：P1 需要建立独立 accumulator 和 legacy adapter。

## ADR-005：事件分为 turn-local 与 session-local 两层序号

- 状态：Accepted
- 决策：`AgentTurnRuntime` 产生 turn-local 单调序号；P4 的 SessionEventBroker 再分配 session-local 单调序号。
- 原因：P1 不应提前依赖全局项目身份；P4 attach/reattach 又需要跨 turn 的稳定游标。
- 影响：事件必须同时携带 `turn_id`，终态事件必须可去重。

## ADR-006：Catalog 是发现投影，不是运行时或会话正文真源

- 状态：Accepted
- 决策：`~/.synapse/catalog.sqlite` 继续只保存项目与会话元数据；项目本地 `.synapse` 数据库是 checkpoint、transcript 和 session 真源。
- 原因：避免全局库成为大体量单点，并保留项目可迁移和独立修复能力。
- 影响：选择全局会话时必须回源验证 workspace 和 thread。

## ADR-007：第一版并行会话使用独立 Agent graph，共享项目级昂贵资源

- 状态：Accepted
- 决策：P5 中每个正在运行的会话拥有独立 Agent graph、SteerQueue 和取消状态；同项目可共享 model client/cache、checkpointer 和 MCP scope。
- 原因：当前 SteerQueue 和部分中间件状态绑定 Agent graph，直接共享 graph 容易串线。
- 影响：P8 必须测量 graph 增量内存，并为 idle runtime 增加回收策略。

## ADR-008：项目切换不修改进程 cwd 和全局环境

- 状态：Accepted
- 决策：backend 通过固定 `root_dir` 工作；项目 `.env` 解析为项目私有 mapping，不使用 `load_dotenv(..., override=True)` 在并行运行期间修改 `os.environ`。
- 原因：进程 cwd 和环境变量是全局可变状态，无法支持并发项目。
- 影响：P6 需要调整 Settings/bootstrap 和 backend 子进程环境构造。

## ADR-009：迁移期保留兼容导出

- 状态：Accepted
- 决策：移动 `stream_agent`、`StreamResult`、normalizer 等实现时，保留现有 `synapse.ui.stream` 等公共导入路径的 re-export，直到调用方和扩展完成迁移。
- 原因：项目指南将包导出视为公共 API；一次性破坏会扩大回归面。
- 影响：每次删除兼容层必须有仓库搜索和迁移说明。

## ADR-010：不在本计划中实现跨程序退出持续运行

- 状态：Accepted
- 决策：运行实例生命周期以当前 Synapse 进程为边界；退出程序统一取消和关闭所有任务。
- 原因：daemon 需要进程管理、鉴权、IPC、重连和版本兼容，是独立产品能力。
- 影响：P8 只验证可靠 shutdown，不实现后台服务。

## ADR-011：统一应用端口位于 Runtime 之上，消费者通过 service contract 接入

- 状态：Accepted
- 决策：新增 `synapse.runtime.service` 传输无关应用服务层（S1 提供 `LocalAgentRuntimeService`：submit turn、查询 session、读取事件、watch replay+live）；S10 的 CLI/TUI/ACP 均通过纯 application DTO ports 接入，复用 RuntimeManager/SessionRuntime 执行栈，不新建执行路径。in-process facade 与 remote client 遵循同一 contract，agent metadata 留在 composition，不走 wire。
- 原因：统一 DTO/端口可服务未来 CLI/TUI/ACP 与网络/daemon 消费者，但执行与生命周期解耦必须继续由既有 P0-P8 栈承担。
- 影响：service 只通过 `RuntimeManager.submit_ref/resume_ref`、`get_session_ref()` 与 `SessionRuntime` 公共方法访问运行态；Local consumer 是 composition owner 而非第二业务 runtime，UI/ACP 不访问 owner.manager。daemon 已由 S8 以 foreground 形态交付；不改变其进程边界。

## ADR-012：S10 consumer cutover 保持单一执行链

- 状态：Accepted
- 决策：CLI 使用 `LocalProjectRuntimeConsumer`，TUI 使用 `TUIRuntimeSessionFacade` 与 `RuntimeEvent` renderer，ACP 使用 service-only `ACPManagedSession`。submit/cancel/steer/approval/watch/status/session switch/dialogs/chrome/steer 均使用 service DTO；TUI UI-only queue 不拥有 execution runtime。watch detach/connection close 只断开观察，不取消 turn；事件使用 session sequence。
- 原因：消费者必须可替换而不复制 runtime 生命周期或业务执行；同一 contract 同时支持进程内 facade 和 remote client。
- 影响：保留 legacy `ui.stream.stream_agent` 作为兼容 utility，但 CLI/TUI/ACP 默认路径不调用 `stream_agent` 或 `agent.ainvoke`。ACP approval 使用 `PendingApprovalQuery`/`ResumeTurnCommand`，checkpoint copy/delete 为纯 callback。
- 验证：实现证据包括安全 ACP 进程内 135 passed、B2 50 passed、C2 433 passed、S6 更新 78 passed、approval service/transport 37 passed；最终全仓安全门禁、MkDocs strict、`uv build` 与 review 尚待完成。

## 决策更新模板

```text
## ADR-NNN：标题

- 状态：Proposed | Accepted | Superseded
- 决策：
- 原因：
- 影响：
- 替代方案：
- 重审条件：
```
